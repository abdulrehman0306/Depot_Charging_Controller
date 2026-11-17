"""
The REST API (FastAPI): see the state of the site, every charger and every van,
and change the site limit while the system runs.

It is served by the controller program itself (see run_controller.py), on
http://127.0.0.1:8000 by default. FastAPI also makes an interactive page with
all endpoints at http://127.0.0.1:8000/docs.

    GET  /status      the site: time, strategy, limit, building, charging and site power
    GET  /chargers    every charger: connected, van, power, allowed power, state of charge
    GET  /vans        every plugged-in van: charger, state of charge, departure, planned shortfall
    PUT  /site-limit  change the grid limit, e.g. {"site_limit_kw": 120}

A new limit is used from the next control cycle (within one simulated minute). If
the site is then above the new planning target, the safety layer cuts charging at once.
"""

import logging

from fastapi import FastAPI
from pydantic import BaseModel, Field

import settings


class SiteLimitChange(BaseModel):
    """The body of PUT /site-limit. FastAPI checks the range and answers 422 if it is wrong."""
    site_limit_kw: float = Field(ge=settings.SITE_LIMIT_MIN_KW, le=settings.SITE_LIMIT_MAX_KW,
                                 description="new grid connection limit for the whole site [kW]")


def charger_status(charger_id, charger):
    """In: charger id and its ChargerConnection. Out: a dict with what the controller knows about it."""
    return {
        "charger_id": charger_id,
        "connected": charger.is_connected,
        "connector_status": charger.connector_status,
        "van_id": charger.van_id,
        "charging_state": charger.charging_state,
        "power_kw": round(charger.charge_power_kw, 2),
        "allowed_power_kw": round(charger.allowed_power_kw, 2),
        "state_of_charge_percent": charger.state_of_charge_percent,
    }


def create_app(controller):
    """
    In: the running SiteController.
    Out: the FastAPI app. Each endpoint reads from (or writes to) that controller.
    """
    app = FastAPI(title="Depot_Charging_Controller API",
                  description="Status of the depot's site, chargers and vans, and the site limit.")
    events = logging.getLogger("API")

    @app.get("/status")
    def get_status():
        """The site right now."""
        last_sample = controller.site_log[-1] if controller.site_log else None
        return {
            "simulated_time": controller.clock.now().isoformat(),
            "strategy": controller.strategy_name,
            "site_limit_kw": controller.site_limit_kw,
            "safety_margin_kw": controller.safety_margin_kw,
            "planning_target_kw": controller.target_kw(),
            "building_kw": controller.building_kw,
            "charging_kw": None if last_sample is None else round(last_sample["charging_kw"], 2),
            "site_kw": None if last_sample is None else round(last_sample["site_kw"], 2),
            "connected_chargers": sum(1 for charger in controller.chargers_by_id.values() if charger.is_connected),
            "safety_actions": len(controller.safety_actions),
        }

    @app.get("/chargers")
    def get_chargers():
        """Every charger that has connected since the controller started."""
        return [charger_status(charger_id, charger) for charger_id, charger in sorted(controller.chargers_by_id.items())]

    @app.get("/vans")
    def get_vans():
        """Every van that is plugged in now."""
        vans = []
        for charger_id, charger in sorted(controller.chargers_by_id.items()):
            if charger.van_id is None:
                continue
            departure_time = controller.fleet_schedule.get(charger.van_id)
            vans.append({
                "van_id": charger.van_id,
                "charger_id": charger_id,
                "state_of_charge_percent": charger.state_of_charge_percent,
                "departure_time": None if departure_time is None else departure_time.isoformat(),
                # Only the smart controller forecasts shortfalls; 0 means it expects the van to make 90 %.
                "planned_shortfall_kwh": controller.planned_shortfall_kwh.get(charger.van_id),
            })
        return vans

    @app.put("/site-limit")
    def put_site_limit(change: SiteLimitChange):
        """Change the grid limit of the whole site."""
        old_limit_kw = controller.site_limit_kw
        controller.site_limit_kw = change.site_limit_kw
        events.warning("site limit changed from %.1f kW to %.1f kW", old_limit_kw, change.site_limit_kw)
        return {"old_site_limit_kw": old_limit_kw, "site_limit_kw": controller.site_limit_kw,
                "planning_target_kw": controller.target_kw()}

    return app
