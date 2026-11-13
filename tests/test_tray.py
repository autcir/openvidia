"""Tray actions control the dashboard independently of the proxy."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from openvidia import __main__ as cli


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in self.callbacks:
            callback(*args)


class Native:
    def __init__(self):
        self.visible = True
        self.minimized = False
        self.raise_ = Mock()
        self.activateWindow = Mock()

    def isVisible(self):
        return self.visible

    def isMinimized(self):
        return self.minimized

    def show(self):
        self.visible = True

    def showNormal(self):
        self.minimized = False
        self.visible = True

    def hide(self):
        self.visible = False


@pytest.fixture
def tray(monkeypatch):
    application = SimpleNamespace(quit=Mock())

    class Action:
        def __init__(self, text):
            self.text = text
            self.triggered = Signal()

    class Menu:
        def __init__(self):
            self.actions = []

        def addAction(self, action):
            self.actions.append(action)

        def addSeparator(self):
            pass

    class Tray:
        ActivationReason = SimpleNamespace(Trigger=1, DoubleClick=2, Context=3)
        isSystemTrayAvailable = staticmethod(lambda: True)

        def __init__(self, icon):
            self.activated = Signal()

        def setToolTip(self, text):
            pass

        def setContextMenu(self, menu):
            self.menu = menu

        def show(self):
            pass

    package = ModuleType("PyQt6")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "PyQt6", package)
    for name, members in {
        "QtCore": {"QCoreApplication": SimpleNamespace(instance=lambda: application)},
        "QtGui": {"QAction": Action, "QIcon": lambda *_: object()},
        "QtWidgets": {"QMenu": Menu, "QSystemTrayIcon": Tray},
    }.items():
        module = ModuleType("PyQt6." + name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, "PyQt6." + name, module)
    monkeypatch.setattr(cli, "_tray_ref", None)
    monkeypatch.setattr(cli, "_tray_hide", None)
    native = Native()
    cli._create_tray("missing-icon.png", SimpleNamespace(native=native), 1919)
    return cli._tray_ref[0], native, application


def test_click_hides_then_restores_dashboard(tray):
    icon, native, application = tray
    icon.activated.emit(icon.ActivationReason.Trigger)
    assert native.visible is False
    icon.activated.emit(icon.ActivationReason.Trigger)
    assert native.visible is True
    native.activateWindow.assert_called_once()
    application.quit.assert_not_called()


def test_show_restores_a_minimized_window(tray):
    icon, native, _ = tray
    native.minimized = True
    icon.activated.emit(icon.ActivationReason.Trigger)
    assert native.visible is True
    assert native.minimized is False
    icon.activated.emit(icon.ActivationReason.DoubleClick)
    assert native.visible is True


def test_menu_has_explicit_hide_and_show_actions(tray):
    icon, native, application = tray
    actions = {action.text: action for action in icon.menu.actions}
    actions["Hide dashboard"].triggered.emit()
    assert native.visible is False
    actions["Show"].triggered.emit()
    assert native.visible is True
    application.quit.assert_not_called()
    icon.activated.emit(icon.ActivationReason.Context)
    assert native.visible is True
    actions["Close dashboard"].triggered.emit()
    application.quit.assert_called_once()
