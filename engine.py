# Copyright (c) 2013 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import os
import sys
import logging
import fnmatch
import logging.handlers

from PySide import QtGui
from PySide import QtCore

from tank.platform import Engine
from tank.errors import TankEngineInitError


class DesktopEngine(Engine):
    def __init__(self, tk, *args, **kwargs):
        """ Constructor """
        # Need to init logging before init_engine to satisfy logging from framework setup
        self._initialize_logging()

        # Now continue with the standard initialization
        Engine.__init__(self, tk, *args, **kwargs)

    def init_engine(self):
        """ Initialize the engine """
        # set logging to the proper level from settings
        if self.get_setting("debug_logging", False):
            self._logger.setLevel(logging.DEBUG)
        else:
            self._logger.setLevel(logging.INFO)

        self.proxy = None
        self.msg_server = None

        self.__first_group_shown = None
        self.__group_headers_visible = False

        self.__callback_map = {}  # mapping from (namespace, name) to callbacks
        self.__app_gui_groups = {}

        # see if we are running with a gui proxy
        self.has_gui = True
        self.has_gui_proxy = False
        bootstrap_data = getattr(self.sgtk, '_desktop_data', None)
        if bootstrap_data is not None:
            if 'proxy_pipe' in bootstrap_data and 'proxy_auth' in bootstrap_data:
                self.has_gui = False
                self.has_gui_proxy = True

        # HACK ALERT: See if we can move this part of engine initialization
        # above the call to init_engine in core
        #
        # try to pull in QT classes and assign to tank.platform.qt.XYZ
        from tank.platform import qt
        base_def = self._define_qt_base()
        qt.QtCore = base_def.get("qt_core")
        qt.QtGui = base_def.get("qt_gui")
        qt.TankDialogBase = base_def.get("dialog_base")

        self.tk_desktop = self.import_module("tk_desktop")

        # we have a gui proxy, connect to it via the data from the bootstrap
        if self.has_gui_proxy:
            pipe = bootstrap_data['proxy_pipe']
            auth = bootstrap_data['proxy_auth']
            self.log_info("Connecting to gui pipe %s" % pipe)

            # create the connection to the gui proxy
            self.proxy = self.tk_desktop.RPCProxy(pipe, auth)

            # startup our server to receive gui calls
            self.start_app_server()

            # get the list of configured groups
            groups = [g['name'] for g in self.get_setting('groups', [])]
            default_group = self.get_setting('default_group', [])

            # add the default group in if it isn't already in the list.
            # it goes at the beginning by default
            if not default_group in groups:
                groups.insert(0, default_group)

            # and register our side of the pipe as the current app proxy
            self.proxy.create_app_proxy(self.msg_server.pipe, self.msg_server.authkey, groups)

    def start_app_server(self):
        """ Start up the app side rpc server """
        if self.msg_server is not None:
            self.msg_server.close()

        self.msg_server = self.tk_desktop.RPCServerThread(self)
        self.msg_server.register_function(self.trigger_callback, 'trigger_callback')
        self.msg_server.register_function(self.open_project_locations, 'open_project_locations')

        self.msg_server.start()

    def start_gui_server(self):
        """ Start up the gui side rpc server """
        if self.msg_server is not None:
            self.msg_server.close()

        self.msg_server = self.tk_desktop.RPCServerThread(self)
        self.msg_server.register_function(self.app_proxy_startup_error, 'engine_startup_error')
        self.msg_server.register_function(self.create_app_proxy, 'create_app_proxy')
        self.msg_server.register_function(self.destroy_app_proxy, 'destroy_app_proxy')
        self.msg_server.register_function(self.trigger_register_command, 'trigger_register_command')
        self.msg_server.register_function(self.trigger_gui, 'trigger_gui')

        self.msg_server.start()

    def destroy_engine(self):
        """ Clean up the engine """
        # clean up our logging setup
        self._tear_down_logging()

        # if we have a gui proxy alert it to the destruction
        if self.has_gui_proxy:
            self.proxy.destroy_app_proxy()
            self.proxy.__close_connection()

        # and close down our server thread
        self.msg_server.close()

    def open_project_locations(self):
        """ Open the project locations in an os specific browser window """
        paths = self.context.filesystem_locations
        for disk_location in paths:
            # get the setting
            system = sys.platform

            # run the app
            if system == "linux2":
                cmd = 'xdg-open "%s"' % disk_location
            elif system == "darwin":
                cmd = 'open "%s"' % disk_location
            elif system == "win32":
                cmd = 'cmd.exe /C start "Folder" "%s"' % disk_location
            else:
                raise Exception("Platform '%s' is not supported." % system)

            exit_code = os.system(cmd)
            if exit_code != 0:
                self.log_error("Failed to launch '%s'!" % cmd)

    ############################################################################
    # App proxy side methods

    def create_app_proxy(self, pipe, authkey, groups):
        self.proxy = self.tk_desktop.RPCProxy(pipe, authkey)
        self._create_group_guis(groups)

    def destroy_app_proxy(self):
        self.desktop_window.clear_app_uis()

    def register_command(self, name, callback, properties):
        self.__callback_map[('__commands', name)] = callback

        # evaluate groups on the app proxy side
        groups = self._get_groups(name, properties)
        self.proxy.trigger_register_command(name, properties, groups)

    def register_callback(self, namespace, command, callback):
        self.__callback_map[(namespace, command)] = callback

    def call_callback(self, namespace, command, *args, **kwargs):
        self.log_debug("calling callback %s::%s(%s, %s)" % (namespace, command, args, kwargs))
        self.proxy.trigger_callback(namespace, command, *args, **kwargs)

    def trigger_callback(self, namespace, command, *args, **kwargs):
        callback = self.__callback_map.get((namespace, command))
        callback(*args, **kwargs)

    def register_gui(self, namespace, callback):
        """
        App side method to register a callback to build a gui when a
        namespace is triggered.
        """
        self.__callback_map[namespace] = callback

    def trigger_build_gui(self, namespace):
        """
        App side method to trigger the building of a GUI for a given namespace.
        """
        self.proxy.trigger_gui(namespace)

    ############################################################################
    # GUI side methods

    def trigger_gui(self, namespace):
        """ GUI side handler for building a custom GUI. """
        callback = self.__callback_map.get(namespace)

        if callback is not None:
            parent = self.desktop_window.get_app_widget(namespace)
            callback(parent)

    def trigger_register_command(self, name, properties, groups):
        """ GUI side handler for the add_command call. """
        self.log_debug("register_command(%s, %s)", name, properties)

        command_type = properties.get('type')
        command_icon = properties.get('icon')

        icon = None
        if command_icon is not None:
            if os.path.exists(command_icon):
                icon = QtGui.QIcon(command_icon)
            else:
                self.log_error("Icon for command '%s' not found: '%s'" % (name, command_icon))

        title = self._get_display_name(name, properties)

        if command_type == 'context_menu':
            # Add the command to the project menu
            menu = self.desktop_window.get_app_menu()
            action = QtGui.QAction(self.desktop_window)
            if icon is not None:
                action.setIcon(icon)
            action.setText(title)

            def action_triggered():
                self.context_menu_app_triggered(name, properties)
            action.triggered.connect(action_triggered)
            menu.addAction(action)
        else:
            # Default is to add an icon/label for the command
            for group in groups:
                # Only show group headers when there are more than one group
                if self.__first_group_shown is None:
                    self.__first_group_shown = group
                elif not self.__group_headers_visible and self.__first_group_shown != group:
                    # more than one group being shown, turn on headers
                    for group_widget in self.__app_gui_groups.values():
                        group_frame = group_widget.parent()
                        expand_button = group_frame.layout().itemAt(0).widget()
                        expand_button.show()
                    self.__group_headers_visible = True

                buttons = self._gui_group_for_app(group)
                buttons.show()

                if icon is None:
                    button = QtGui.QPushButton(title)
                else:
                    button = QtGui.QPushButton(icon, title)

                button.setIconSize(QtCore.QSize(42, 42))
                button.setFlat(True)
                button.setStyleSheet("""
                    text-align: left;
                    font-size: 14px;
                    background-color: transparent;
                    border: none;
                """)

                def button_clicked():
                    self.button_app_triggered(name, properties)
                button.clicked.connect(button_clicked)

                # find out where the button goes
                # based off of current widget count
                layout = buttons.layout()
                (row, column) = divmod(layout.count(), 2)
                layout.addWidget(button, row, column)

    def context_menu_app_triggered(self, name, properties):
        """ App triggered from the project specific menu. """
        # make sure to pass in that we are not expecting a response
        # Especially for the engine restart command, the connection
        # itself gets reset and so there isn't a channel to get a
        # response back.
        self.proxy.trigger_callback('__commands', name, __proxy_expected_return=False)

    def button_app_triggered(self, name, properties):
        """ Button clicked from a registered command. """
        self.proxy.trigger_callback('__commands', name, __proxy_expected_return=False)

    def app_proxy_startup_error(self, error):
        """ Handle an error starting up the engine for the app proxy. """
        parent = self.desktop_window.get_app_widget("___error")
        if isinstance(error, TankEngineInitError):
            label = QtGui.QLabel(
                "<big>Error starting engine</big>"
                "<br/><br/>%s" % error.message)
        else:
            label = QtGui.QLabel(
                "<big>Unknown Error</big>"
                "<br/><br/>%s - %s" % (type(error), error.message))

        label.setWordWrap(True)
        label.setMargin(15)
        index = parent.layout().count() - 1
        parent.layout().insertWidget(index, label)

    def clear_app_groups(self):
        self.__app_gui_groups = {}

    def _get_display_name(self, name, properties):
        return properties.get('title', name)

    def _get_groups(self, name, properties):
        display_name = self._get_display_name(name, properties)

        default_group = self.get_setting('default_group', 'Studio')
        groups = self.get_setting('groups', [])

        matches = []
        for group in groups:
            for match in group['matches']:
                if fnmatch.fnmatch(display_name, match):
                    matches.append(group['name'])
                    break

        if not matches:
            matches = [default_group]

        self.log_debug("'%s' goes in groups: %s" % (display_name, matches))
        return matches

    def _create_group_guis(self, groups):
        parent = self.desktop_window.get_app_widget('__commands')

        for group in groups:
            group_box = QtGui.QFrame(parent)
            group_box.setStyleSheet("""
                border: 1px solid gray;
                border-top: none;
                border-left: none;
                border-right: none;
                """)

            # add button to expand collapse widget
            expand_button = QtGui.QPushButton(self.desktop_window.down_arrow, group)
            expand_button.setFlat(True)
            expand_button.setStyleSheet("""
                    text-align: left;
                    font-size: 14px;
                    background-color: transparent;
                    border: none;
                """)
            expand_button.hide()

            # the container for the app widgets
            app_widget = QtGui.QWidget(group_box)

            group_layout = QtGui.QVBoxLayout()
            group_layout.addWidget(expand_button)
            group_layout.addWidget(app_widget)
            group_box.setLayout(group_layout)

            app_layout = QtGui.QGridLayout()
            app_widget.setLayout(app_layout)

            index = parent.layout().count() - 1
            parent.layout().insertWidget(index, group_box)

            def toggle_group():
                if app_widget.isHidden():
                    app_widget.show()
                    expand_button.setIcon(self.desktop_window.down_arrow)
                else:
                    app_widget.hide()
                    expand_button.setIcon(self.desktop_window.right_arrow)
            expand_button.clicked.connect(toggle_group)
            app_widget.hide()

            self.__app_gui_groups[group] = app_widget

    def _gui_group_for_app(self, group):
        """
        Return the parent widget for each grouping of apps.

        Construct the widget for each group as it is needed.
        """
        return self.__app_gui_groups[group]

    def run(self):
        """
        Run the engine.

        This method is called from the GUI bootstrap to setup the application
        and to run the Qt event loop.
        """
        # Initialize PySide app
        self.app = QtGui.QApplication.instance()
        if self.app is None:
            # setup the stylesheet
            QtGui.QApplication.setStyle("cleanlooks")
            self.app = QtGui.QApplication(sys.argv)
            css_file = os.path.join(self.disk_location, "resources", "dark.css")
            f = open(css_file)
            css = f.read()
            f.close()
            self.app.setStyleSheet(css)

        # update the app icon
        icon = QtGui.QIcon(":res/default_systray_icon")
        self.app.setWindowIcon(icon)

        # initialize System Tray
        self.desktop_window = self.tk_desktop.DesktopWindow()

        # and run the app
        return self.app.exec_()

    ############################################################################
    # Logging

    def _initialize_logging(self):
        # platform specific locations for the log file
        platform_lookup = {
            'darwin': os.path.join(os.path.expanduser("~"), "Library", "Logs", "Shotgun", "tk-desktop.log"),
            'win32': os.path.join(os.environ.get("APPDATA", "APPDATA_NOT_SET"), "Shotgun", "tk-desktop.log"),
            'linux': None,
        }
        fname = platform_lookup.get(sys.platform)
        if fname is None:
            raise NotImplementedError("Unknown platform: %s" % sys.platform)

        # create the directory for the log file
        log_dir = os.path.dirname(fname)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        # setup default logger, used in the new default exception hook
        self._logger = logging.getLogger("tk-desktop")
        self._handler = logging.handlers.RotatingFileHandler(fname, maxBytes=1024*1024, backupCount=5)
        formatter = logging.Formatter('%(asctime)s %(process)d [%(levelname)s] %(name)s - %(message)s')
        self._handler.setFormatter(formatter)
        self._logger.addHandler(self._handler)

    def _tear_down_logging(self):
        # clear the handler so we don't end up with duplicate messages
        self._logger.removeHandler(self._handler)

    def log_debug(self, msg, *args):
        self._logger.debug(msg, *args)

    def log_info(self, msg, *args):
        self._logger.info(msg, *args)

    def log_warning(self, msg, *args):
        self._logger.warn(msg, *args)

    def log_error(self, msg, *args):
        self._logger.error(msg, *args)
