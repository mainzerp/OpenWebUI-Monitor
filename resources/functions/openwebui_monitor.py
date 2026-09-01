"""
title: Usage Monitor
author: VariantConst & OVINC CN / mainzerp
git_url: https://github.com/mainzerp/OpenWebUI-Monitor.git
version: 0.3.9
requirements: httpx
license: MIT
"""

import logging
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
        "cost": "Cost: ${cost:.4f}",
        "balance": "Balance: ${balance:.4f}",
        "tokens": "Tokens: {input}+{output}",
        "time_spent": "Time: {time:.2f}s",
        "tokens_per_sec": "{tokens_per_sec:.2f} T/s",
    },
    "de": {
        "request_failed": "Anfrage fehlgeschlagen: {error_msg}",
        "insufficient_balance": (
            "Unzureichendes Guthaben: Aktuelles Guthaben `{balance:.4f}`"
        ),
        "cost": "Kosten: ${cost:.4f}",
        "balance": "Guthaben: ${balance:.4f}",
        "tokens": "Tokens: {input}+{output}",
        "time_spent": "Zeit: {time:.2f}s",
        "tokens_per_sec": "{tokens_per_sec:.2f} T/s",
    },
    "zh": {
        "request_failed": "请求失败: {error_msg}",
        "insufficient_balance": "余额不足: 当前余额 `{balance:.4f}`",
        "cost": "费用: ¥{cost:.4f}",
        "balance": "余额: ¥{balance:.4f}",
        "tokens": "Token: {input}+{output}",
        "time_spent": "耗时: {time:.2f}s",
        "tokens_per_sec": "{tokens_per_sec:.2f} T/s",
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
        show_time_spent: bool = Field(
            default=True,
            description="Show elapsed time",
        )
        show_tokens_per_sec: bool = Field(
            default=True,
            description="Show output tokens per second",
        )
        show_cost: bool = Field(
            default=True,
            description="Show cost",
        )
        show_balance: bool = Field(
            default=True,
            description="Show remaining balance",
        )
        show_tokens: bool = Field(
            default=True,
            description="Show input and output tokens",
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

        This method is intentionally not called 'request' to avoid possible
        name conflicts with OpenWebUI or newer filter implementations.
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
        metadata = __metadata__ or {}

        # Metadata is currently not required by the Monitor API.
        del metadata

        user_id = self._get_user_id(user)
        self.start_times[user_id] = time.time()

        # Remove trailing slashes from the endpoint.
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
        metadata = __metadata__ or {}

        # Metadata is currently not required by the Monitor API.
        del metadata

        user_id = self._get_user_id(user)

        # Do not send an outlet request if inlet blocked the request.
        if self.outage_map.get(user_id, False):
            return body

        endpoint = self.valves.api_endpoint.rstrip("/")

        if not endpoint:
            return body

        client = AsyncClient(timeout=Timeout(30.0))

        try:
            response_data = await self._send_request(
                client=client,
                url=f"{endpoint}/api/v1/outlet",
                headers={
                    "Authorization": f"Bearer {self.valves.api_key}",
                    "Content-Type": "application/json",
                },
                payload={
                    "user": user,
                    "body": body,
                },
            )

            stats = []

            input_tokens = int(
                response_data.get("inputTokens", 0)
            )
            output_tokens = int(
                response_data.get("outputTokens", 0)
            )
            total_cost = float(
                response_data.get("totalCost", 0)
            )
            new_balance = float(
                response_data.get("newBalance", 0)
            )

            if self.valves.show_tokens:
                stats.append(
                    self.get_text(
                        "tokens",
                        input=input_tokens,
                        output=output_tokens,
                    )
                )

            if self.valves.show_cost:
                stats.append(
                    self.get_text(
                        "cost",
                        cost=total_cost,
                    )
                )

            if self.valves.show_balance:
                stats.append(
                    self.get_text(
                        "balance",
                        balance=new_balance,
                    )
                )

            start_time = self.start_times.get(user_id)

            if start_time and self.valves.show_time_spent:
                elapsed = time.time() - start_time

                stats.append(
                    self.get_text(
                        "time_spent",
                        time=elapsed,
                    )
                )

                if self.valves.show_tokens_per_sec:
                    tokens_per_second = (
                        output_tokens / elapsed
                        if elapsed > 0
                        else 0
                    )

                    stats.append(
                        self.get_text(
                            "tokens_per_sec",
                            tokens_per_sec=tokens_per_second,
                        )
                    )

            description = " | ".join(stats)

            if __event_emitter__ and description:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": description,
                            "done": True,
                        },
                    }
                )

            logger.info(
                "[Monitor] user=%s stats=%s",
                user_id,
                description,
            )

            return body

        except Exception as error:
            # Outlet errors must not block the model response.
            logger.exception(
                "[Monitor] outlet error (non-blocking): %s",
                error,
            )
            return body

        finally:
            await client.aclose()

            # Clean up request-specific state.
            self.start_times.pop(user_id, None)
            self.outage_map.pop(user_id, None)
