"""REST API blueprints."""
from __future__ import annotations

from typing import Any, Dict, Optional

from flask import Flask, jsonify, request

from ..config import ConfigError
from ..gpio.common import EStopEngaged, GPIOError
from ..wled.client import WLEDError
from ..wled.manager import DeviceError


class APIError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


def body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if data is None:
        if request.form:
            return dict(request.form)
        return {}
    if not isinstance(data, dict):
        raise APIError("request body must be a JSON object")
    return data


def ok(payload: Optional[Any] = None, status: int = 200, **kwargs):
    if payload is None:
        payload = {"ok": True}
    if kwargs and isinstance(payload, dict):
        payload = {**payload, **kwargs}
    return jsonify(payload), status


def register_blueprints(app: Flask) -> None:
    from . import dashboard, devices, estop, gpio, system, ui

    app.register_blueprint(ui.bp)
    app.register_blueprint(devices.bp, url_prefix="/api")
    app.register_blueprint(gpio.bp, url_prefix="/api")
    app.register_blueprint(estop.bp, url_prefix="/api")
    app.register_blueprint(dashboard.bp, url_prefix="/api")
    app.register_blueprint(system.bp, url_prefix="/api")

    @app.errorhandler(APIError)
    def _api_error(exc: APIError):
        return jsonify({"error": exc.message, **exc.extra}), exc.status

    @app.errorhandler(DeviceError)
    def _device_error(exc):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(WLEDError)
    def _wled_error(exc):
        return jsonify({"error": str(exc)}), 502

    @app.errorhandler(EStopEngaged)
    def _estop_engaged(exc: EStopEngaged):
        # 409: the request is valid, the system is just latched out.  The zone
        # lets the dashboard offer the right "reset" button straight away.
        return jsonify({"error": str(exc), "estop": True,
                        "zone": exc.zone_id, "zone_name": exc.zone_name}), 409

    @app.errorhandler(GPIOError)
    def _gpio_error(exc):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(ConfigError)
    def _config_error(exc):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(ValueError)
    def _value_error(exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": f"invalid value: {exc}"}), 400
        raise exc

    @app.errorhandler(TypeError)
    def _type_error(exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "invalid value"}), 400
        raise exc

    @app.errorhandler(404)
    def _not_found(exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return exc

    @app.errorhandler(405)
    def _method(exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "method not allowed"}), 405
        return exc

    @app.errorhandler(500)
    def _server_error(exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal error, check the logs"}), 500
        return exc
