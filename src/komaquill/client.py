"""ComfyUI標準HTTP APIへの通信。"""

from json import JSONDecodeError
from typing import Any, cast
from urllib.parse import quote

import httpx

from komaquill.errors import KomaquillError, SubmissionUnknown
from komaquill.models import ImageRef, JsonObject


class ComfyClient:
    """リダイレクトせず認証情報を接続先だけに送る。"""

    def __init__(
        self,
        endpoint: str,
        password: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        *,
        username: str | None = None,
    ) -> None:
        self.http = httpx.Client(
            base_url=endpoint,
            auth=httpx.BasicAuth(username or "komaquill", password)
            if password is not None
            else None,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        """接続を閉じる。"""
        self.http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """エラー応答の本文や資格情報を表示しない。"""
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            if method == "POST" and path == "/prompt":
                raise SubmissionUnknown(
                    "受付結果が不明です。collect で履歴を照合してください。"
                ) from exc
            raise KomaquillError(
                "ComfyUIへ接続できません。接続先と start の実行状態を確認してください。",
                code="connection_failed",
                stage="communication",
            ) from exc
        if response.status_code == 401:
            raise KomaquillError(
                "認証に失敗しました。start でパスワードを設定し直してください。",
                code="authentication_failed",
                stage="communication",
                http_status=401,
                retryable=False,
            )
        if method == "POST" and path == "/prompt" and response.status_code >= 500:
            raise SubmissionUnknown("受付結果が不明です。collect で履歴を照合してください。")
        if not response.is_success:
            raise KomaquillError(
                f"ComfyUIの操作に失敗しました（HTTP {response.status_code}）。",
                code="http_failed",
                stage="communication",
                http_status=response.status_code,
            )
        return response

    def json(self, path: str) -> JsonObject:
        """オブジェクト形式の応答を読み込む。"""
        try:
            data = self.request("GET", path).json()
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            raise KomaquillError(
                "ComfyUIの応答形式が想定と異なります。",
                code="invalid_response",
                stage="communication",
            ) from exc
        if not isinstance(data, dict):
            raise KomaquillError(
                "ComfyUIの応答形式が想定と異なります。",
                code="invalid_response",
                stage="communication",
            )
        return cast(JsonObject, data)

    def queue(self) -> list[list[Any]]:
        """標準キューの行を検証し、応答本文を例外へ含めない。"""
        data = self.json("/queue")
        result: list[list[Any]] = []
        for key in ("queue_running", "queue_pending"):
            rows = data.get(key, [])
            if not isinstance(rows, list):
                raise KomaquillError("ComfyUIのキュー形式を確認できません。")
            for row in rows:
                if not isinstance(row, list) or len(row) < 3 or not isinstance(row[1], str):
                    raise KomaquillError("ComfyUIのキュー形式を確認できません。")
                result.append(row)
        return result

    def history(self) -> JsonObject:
        """現在のランタイムの履歴を取得する。"""
        return self.json("/history")

    def runtime(self) -> JsonObject:
        """準備スクリプトが標準userdataへ保存した識別情報を取得する。"""
        return self.json("/userdata/" + quote("komaquill/session.json", safe=""))

    def submit(self, graph: JsonObject, ui: JsonObject, run_id: str) -> str:
        """生成要求を送信する。通信断では受付不明として扱う。"""
        response = self.request(
            "POST",
            "/prompt",
            json={
                "prompt": graph,
                "client_id": run_id,
                "extra_data": {"komaquill": {"run_id": run_id}, "extra_pnginfo": {"workflow": ui}},
            },
        )
        try:
            data = response.json()
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            raise SubmissionUnknown(
                "生成IDを確認できません。collect で履歴を照合してください。"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("prompt_id"), str):
            raise SubmissionUnknown("生成IDを確認できません。collect で履歴を照合してください。")
        return cast(str, data["prompt_id"])

    def image(self, reference: ImageRef) -> bytes:
        """履歴に記録された画像を取得する。"""
        return self.request("GET", "/view", params=reference.model_dump()).content
