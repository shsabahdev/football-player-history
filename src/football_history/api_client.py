"""Rate-limited API-Football HTTP client."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .config import Config
from .database import Database
from .models import CheckpointState


class DailyLimitReached(RuntimeError):
    """Raised before a request would exceed the configured daily quota."""


class APIRequestError(RuntimeError):
    """Raised for API request failures that cannot be retried."""


class APIClient:
    """Perform authenticated, retried requests and persist usage information."""

    def __init__(
        self,
        config: Config,
        database: Database,
        checkpoint: CheckpointState,
        save_checkpoint: Callable[[CheckpointState], None],
        logger: logging.Logger | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.checkpoint = checkpoint
        self.save_checkpoint = save_checkpoint
        self.logger = logger or logging.getLogger("football_history")
        self.session = session or requests.Session()
        self.last_raw_json_path: Path | None = None
        player = database.get_player(checkpoint.player_name)
        self.request_context: dict[str, Any] = {
            "player_id": int(player["id"]) if player else None,
            "provider_player_id": checkpoint.provider_player_id,
            "season": checkpoint.current_season,
            "fixture_id": checkpoint.active_fixture_id,
            "stage": checkpoint.current_stage,
            "team_id": None,
            "team_name": None,
            "league_id": None,
            "league_name": None,
            "page_number": None,
        }
        self.session.headers.update({"x-apisports-key": config.api_key})

    def set_request_context(
        self,
        *,
        player_id: int | None = None,
        provider_player_id: str | None = None,
        season: int | None = None,
        fixture_id: str | None = None,
        stage: str | None = None,
        team_id: str | None = None,
        team_name: str | None = None,
        league_id: str | None = None,
        league_name: str | None = None,
        page_number: int | None = None,
    ) -> None:
        """Set attribution metadata applied to subsequent requests."""
        self.request_context = {
            "player_id": player_id,
            "provider_player_id": provider_player_id,
            "season": season,
            "fixture_id": fixture_id,
            "stage": stage,
            "team_id": team_id,
            "team_name": team_name,
            "league_id": league_id,
            "league_name": league_name,
            "page_number": page_number,
        }

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch JSON with retry/backoff and request quota tracking."""
        self._reset_daily_usage()
        url = f"{self.config.api.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        attempts = self.config.api.max_retries + 1
        last_error = "Unknown API request error"
        effective_limit = max(
            0,
            self.config.api.requests_per_day - self.config.api.daily_request_safety_margin,
        )
        for attempt in range(attempts):
            today = datetime.now(timezone.utc).date().isoformat()
            reservation = self.database.reserve_daily_request(
                today, effective_limit, self.checkpoint.player_name
            )
            if reservation is None:
                self.checkpoint.request_date = today
                self.checkpoint.requests_used_today = (
                    self._player_usage(today)
                )
                self.checkpoint.status = "paused"
                self.save_checkpoint(self.checkpoint)
                raise self._budget_exhausted(today, effective_limit)
            _, player_usage = reservation
            self.checkpoint.request_date = today
            self.checkpoint.requests_used_today = player_usage
            self.save_checkpoint(self.checkpoint)
            if self.config.crawling.delay_between_requests_seconds:
                time.sleep(self.config.crawling.delay_between_requests_seconds)
            response: requests.Response | None = None
            request_id = self.database.begin_request(
                self.config.api.provider, endpoint, url, **self.request_context,
            )
            try:
                response = self.session.get(
                    url, params=params, timeout=self.config.api.request_timeout_seconds
                )
                if response.status_code in (401, 403, 404):
                    last_error = f"HTTP {response.status_code} for {endpoint}"
                    self.database.finish_request(request_id, response.status_code, False, last_error)
                    raise APIRequestError(last_error)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"Transient HTTP {response.status_code} for {endpoint}"
                    self.database.finish_request(request_id, response.status_code, False, last_error)
                    if attempt + 1 < attempts:
                        self.logger.warning(
                            "%s; retrying attempt %s/%s", last_error, attempt + 2, attempts
                        )
                        self._backoff(attempt)
                        continue
                    raise APIRequestError(last_error)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    self.database.finish_request(
                        request_id, response.status_code, False, "Unexpected response format"
                    )
                    raise APIRequestError(f"Unexpected response format from {endpoint}")
                api_errors = payload.get("errors")
                api_error_message = (
                    f"API error: {api_errors}" if api_errors and api_errors != {} else None
                )
                self.database.finish_request(
                    request_id, response.status_code, api_error_message is None, api_error_message
                )
                if self.config.crawling.save_raw_json:
                    self.last_raw_json_path = self._save_raw(endpoint, params, payload)
                else:
                    self.last_raw_json_path = None
                return payload
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"{type(exc).__name__} for {endpoint}: {exc}"
                self.database.finish_request(
                    request_id, response.status_code if response is not None else None,
                    False, type(exc).__name__,
                )
                if attempt + 1 < attempts:
                    self._backoff(attempt)
                    continue
                raise APIRequestError(last_error) from exc
            except requests.HTTPError as exc:
                last_error = f"HTTP error for {endpoint}: {exc}"
                self.database.finish_request(
                    request_id, response.status_code if response is not None else None, False, last_error
                )
                raise APIRequestError(last_error) from exc
            except requests.RequestException as exc:
                last_error = f"Request error for {endpoint}: {type(exc).__name__}"
                self.database.finish_request(
                    request_id, response.status_code if response is not None else None,
                    False, type(exc).__name__,
                )
                if attempt + 1 < attempts:
                    self._backoff(attempt)
                    continue
                raise APIRequestError(last_error) from exc
            except ValueError as exc:
                last_error = f"Invalid JSON response from {endpoint}"
                self.database.finish_request(
                    request_id, response.status_code if response is not None else None, False, last_error
                )
                raise APIRequestError(last_error) from exc
            except APIRequestError:
                raise
        raise APIRequestError(last_error)

    def _reset_daily_usage(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        usage = self._player_usage(today)
        if self.checkpoint.request_date != today or self.checkpoint.requests_used_today != usage:
            self.checkpoint.request_date = today
            self.checkpoint.requests_used_today = usage
            self.save_checkpoint(self.checkpoint)

    def _player_usage(self, request_date: str) -> int:
        return self.database.get_daily_request_usage_for_player(
            request_date, self.checkpoint.player_name
        )

    def _budget_exhausted(self, today: str, effective_limit: int) -> DailyLimitReached:
        reset_date = datetime.fromisoformat(today).date() + timedelta(days=1)
        reset_at = datetime.combine(reset_date, datetime_time.min, tzinfo=timezone.utc)
        global_used = self.database.get_daily_request_usage(today)
        season = self.checkpoint.current_season or "not started"
        fixture = self.checkpoint.active_fixture_id or "not started"
        return DailyLimitReached(
            f"Daily API request budget reached: {global_used}/{effective_limit} requests used "
            f"(configured {self.config.api.requests_per_day}, safety margin "
            f"{self.config.api.daily_request_safety_margin}). Paused player "
            f"'{self.checkpoint.player_name}', season {season}, fixture {fixture}, "
            f"stage {self.checkpoint.current_stage}. Budget resets at "
            f"{reset_at.isoformat()}."
        )

    def _backoff(self, attempt: int) -> None:
        time.sleep(self.config.api.retry_backoff_seconds * (2 ** attempt))

    def _save_raw(
        self, endpoint: str, params: dict[str, Any] | None, payload: dict[str, Any]
    ) -> Path:
        identity = json.dumps({"endpoint": endpoint, "params": params or {}}, sort_keys=True)
        digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
        endpoint_name = "_".join(part for part in endpoint.split("/") if part)
        slug = re.sub(r"[^a-z0-9]+", "_", self.checkpoint.player_name.casefold()).strip("_")
        output = self.config.paths.json / (slug or "player") / f"{endpoint_name}_{digest}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(output)
        return output
