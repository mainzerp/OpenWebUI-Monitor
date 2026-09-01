"""
title: Usage Monitor (Invisible)
author: VariantConst & OVINC CN / mainzerp
git_url: https://github.com/mainzerp/OpenWebUI-Monitor.git
version: 0.3.9
requirements: httpx
license: MIT
"""

import json
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from httpx import AsyncClient, Timeout
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


TRANSLATIONS = {
    "en": {
        "request_failed": "Request failed: {error_msg}",
        "insufficient_balance": (
            "Insufficient balance: Current balance `{balance:.4f}`"
        ),
    },
    "de": {
        "request_failed": "Anfrage fehlgeschlagen: {error_msg}",
        "insufficient_balance": (
            "Unzureichendes Guthaben: Aktuelles Guthaben `{balance:.4f}`"
        ),
    },
    "zh": {
        "request_failed": "请求失败: {error_msg}",
        "insufficient_balance": "余额不足: 当前余额 `{balance:.4f}`",
    },
}


class CustomException(Exception):
    pass


class Filter:
    class Valves(BaseModel):
        api_endpoint: str = Field(
            default="",
            description="Base URL of the OpenWebUI Monitor backend",
        )
        api_key: str = Field(
            default="",
            description="API key configured in the Monitor backend",
        )
        priority: int = Field(
            default=5,
            description="Filter priority",
        )
        language: str = Field(
            default="en",
            description="Language: de, en or zh",
        )
        record_directory: str = Field(
            default="/app/backend/data/record",
            description="Directory where per-message usage records are stored",
        )

    def __init__(self):
        self.type = "filter"
        self.name = "OpenWebUI Monitor"
        self.valves = self.Valves()

        # Stores the balance status for each user.
        self.outage_map: Dict[str, bool] = {}

        # Stores the start time for each user.
        # A global start_time would cause problems with parallel requests.
        self.start_times: Dict[str, float] = {}

        # Stores the inlet body for each user, needed to merge message history in outlet.
        self.inlet_bodies: Dict[str, dict] = {}

    def get_text(self, key: str, **kwargs: Any) -> str:
        language = self.valves.language.lower()

        if language not in TRANSLATIONS:
            language = "en"

        text = TRANSLATIONS[language].get(
            key,
            TRANSLATIONS["en"].get(key, key),
        )

        return text.format(**kwargs) if kwargs else text

    def _make_json_serializable(self, value: Any) -> Any:
        """
        Convert Pydantic objects and nested values into JSON-compatible data.
        Supports both Pydantic v1 and v2.
        """
        if hasattr(value, "model_dump"):
            return value.model_dump()

        if hasattr(value, "dict"):
            return value.dict()

        if isinstance(value, dict):
            return {
                key: self._make_json_serializable(item)
                for key, item in value.items()
            }

        if isinstance(value, list):
            return [
                self._make_json_serializable(item)
                for item in value
            ]

        if isinstance(value, tuple):
            return [
                self._make_json_serializable(item)
                for item in value
            ]

        return value

    async def _send_request(
        self,
        client: AsyncClient,
        url: str,
        headers: dict,
        payload: dict,
    ) -> dict:
        """
        Send an HTTP request to the OpenWebUI Monitor backend.
        """
        json_payload = self._make_json_serializable(payload)

        response = await client.post(
            url=url,
            headers=headers,
            json=json_payload,
        )

        response.raise_for_status()
        response_data = response.json()

        if not response_data.get("success"):
            error_message = self.get_text(
                "request_failed",
                error_msg=response_data,
            )

            logger.error(error_message)
            raise CustomException(error_message)

        return response_data

    def _get_user_id(self, user: dict) -> str:
        return str(user.get("id", "default"))

    def _modify_outlet_body(
        self,
        body: dict,
        inlet_body: Optional[dict],
    ) -> dict:
        """
        Merge the original inlet messages into the outlet body if the response message
        does not contain the info field (e.g. regenerated answers), so token
        counting uses the original conversation context.
        """
        body_modified = dict(body)
        messages = body_modified.get("messages") or []
        inlet_body = inlet_body or {}

        last_message = messages[-1] if messages else {}

        if "info" not in last_message and inlet_body.get("messages"):
            inlet_messages = inlet_body.get("messages", [])
            body_modified["messages"][:-1] = inlet_messages

        return body_modified

    async def _emit_error(
        self,
        __event_emitter__: Optional[Callable],
        description: str,
    ) -> None:
        if not __event_emitter__:
            return

        await __event_emitter__(
            {
                "type": "status",
                "data": {
                    "description": description,
                    "done": True,
                },
            }
        )

    async def inlet(
        self,
        body: dict,
        __metadata__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        """
        Run before the model request.
        """
        user = __user__ or {}

        user_id = self._get_user_id(user)
        self.start_times[user_id] = time.time()

        # Keep the inlet body so the outlet can merge the original conversation.
        self.inlet_bodies[user_id] = self._make_json_serializable(body)

        endpoint = self.valves.api_endpoint.rstrip("/")

        if not endpoint:
            logger.warning(
                "[Monitor] No API endpoint configured. "
                "The request will not be blocked."
            )
            return body

        client = AsyncClient(timeout=Timeout(30.0))

        try:
            response_data = await self._send_request(
                client=client,
                url=f"{endpoint}/api/v1/inlet",
                headers={
                    "Authorization": f"Bearer {self.valves.api_key}",
                    "Content-Type": "application/json",
                },
                payload={
                    "user": user,
                    "body": body,
                },
            )

            balance = float(response_data.get("balance", 0))
            self.outage_map[user_id] = balance <= 0

            if self.outage_map[user_id]:
                error_message = self.get_text(
                    "insufficient_balance",
                    balance=balance,
                )

                logger.info(error_message)
                raise CustomException(error_message)

            return body

        except CustomException:
            # Balance errors must block the request.
            raise

        except Exception as error:
            # Network and Monitor errors must not block OpenWebUI.
            logger.exception(
                "[Monitor] inlet error (non-blocking): %s",
                error,
            )
            return body

        finally:
            await client.aclose()

    async def outlet(
        self,
        body: dict,
        __metadata__: Optional[dict] = None,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> dict:
        """
        Run after the model response.
        """
        user = __user__ or {}

        user_id = self._get_user_id(user)

        # Do not send an outlet request if inlet blocked the request.
        if self.outage_map.get(user_id, False):
            return body

        endpoint = self.valves.api_endpoint.rstrip("/")

        if not endpoint:
            return body

        client = AsyncClient(timeout=Timeout(30.0))

        try:
            outlet_body = self._modify_outlet_body(
                body,
                self.inlet_bodies.get(user_id),
            )

            response_data = await self._send_request(
                client=client,
                url=f"{endpoint}/api/v1/outlet",
                headers={
                    "Authorization": f"Bearer {self.valves.api_key}",
                    "Content-Type": "application/json",
                },
                payload={
                    "user": user,
                    "body": outlet_body,
                },
            )

            # Build the usage statistics.
            stats_data = {
                "input_tokens": int(
                    response_data.get("inputTokens", 0)
                ),
                "output_tokens": int(
                    response_data.get("outputTokens", 0)
                ),
                "total_cost": float(
                    response_data.get("totalCost", 0)
                ),
                "new_balance": float(
                    response_data.get("newBalance", 0)
                ),
            }

            # Calculate elapsed time if available.
            start_time = self.start_times.get(user_id)

            if start_time:
                elapsed = time.time() - start_time

                stats_data["elapsed_time"] = elapsed

                output_tokens = stats_data["output_tokens"]
                stats_data["tokens_per_sec"] = (
                    output_tokens / elapsed if elapsed > 0 else 0
                )

            # Persist the statistics for the given message.
            messages = body.get("messages", [])
            message_id = (
                messages[-1].get("id") if messages else None
            )

            if message_id:
                # Ensure the record directory exists.
                os.makedirs(
                    self.valves.record_directory,
                    exist_ok=True,
                )

                file_path = os.path.join(
                    self.valves.record_directory,
                    f"{message_id}.json",
                )

                # Persist the statistics as a JSON file.
                with open(file_path, "w") as file:
                    json.dump(stats_data, file, indent=4)

                logger.info(
                    "[Monitor] user=%s message=%s recorded",
                    user_id,
                    message_id,
                )
            else:
                logger.warning(
                    "[Monitor] user=%s: could not extract message ID",
                    user_id,
                )

            return body

        except CustomException:
            return body

        except Exception as error:
            # Outlet errors must not block the model response.
            logger.exception(
                "[Monitor] outlet error (non-blocking): %s",
                error,
            )

            await self._emit_error(
                __event_emitter__,
                self.get_text(
                    "request_failed",
                    error_msg=str(error),
                ),
            )

            return body

        finally:
            await client.aclose()

            # Clean up request-specific state.
            self.start_times.pop(user_id, None)
            self.outage_map.pop(user_id, None)
            self.inlet_bodies.pop(user_id, None)
