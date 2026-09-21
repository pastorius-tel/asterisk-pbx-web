"""AMI (Asterisk Manager Interface) クライアント。

Asterisk への管理コマンド送信用 (reload / peer の状態取得など)。
標準ライブラリの asyncio だけで実装。

実装上のポイント:
  - 各リクエストに ActionID を付け、レスポンスを ID で紐付ける。
  - AMI は Action 直後にレスポンスを返した後も Event (例: 'Event: Reload',
    'Event: FullyBooted') を非同期に流してくる。これらは無視する。
    旧実装はこの Event を Action のレスポンスと取り違えて誤判定していた。
  - 'Response: Success' は受付完了、'Response: Error' は明確な失敗、
    'Response: Follows' は CLI コマンド出力が続くケース (成功扱い)。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class AmiResponse:
    success: bool
    message: str
    raw: str


class AmiClient:
    """軽量 AMI クライアント。

    使い方::

        async with AmiClient() as ami:
            resp = await ami.reload_module("res_pjsip.so")
            print(resp.message)
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        secret: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.host = host or settings.ami_host
        self.port = port or settings.ami_port
        self.user = user or settings.ami_user
        self.secret = secret or settings.ami_secret
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> AmiClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        # バナー (例: "Asterisk Call Manager/2.10.0") を読み捨て
        await asyncio.wait_for(self._reader.readline(), timeout=self.timeout)

        action_id = self._new_action_id()
        await self._send({
            "Action": "Login",
            "Username": self.user,
            "Secret": self.secret,
            "ActionID": action_id,
        })
        resp = await self._read_response_for(action_id)
        if "Response: Success" not in resp:
            raise RuntimeError(f"AMI ログイン失敗: {resp!r}")

    async def close(self) -> None:
        if self._writer is not None:
            try:
                action_id = self._new_action_id()
                await self._send({"Action": "Logoff", "ActionID": action_id})
            except Exception:  # noqa: BLE001
                pass
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
            self._reader = None

    async def action(self, fields: dict[str, str]) -> AmiResponse:
        """AMI Action を実行。``Response: Success/Follows`` で成功判定。"""
        action_id = self._new_action_id()
        fields = {**fields, "ActionID": action_id}
        await self._send(fields)
        raw = await self._read_response_for(action_id)
        # 成功判定:
        #   Response: Success     … 通常の Action 成功
        #   Response: Follows     … CLI コマンド出力が続くタイプ (成功)
        #   "Command output follows" … 一部 Asterisk が返す success 相当
        #   Privilege: Command    … features reload 等で時々返るパターン
        success = (
            "Response: Success" in raw
            or "Response: Follows" in raw
            or "Command output follows" in raw
            or "Privilege: Command" in raw
        ) and "Response: Error" not in raw
        msg = self._extract_message(raw)
        # メッセージが空なら raw 全文の代わりに「OK」を返す (NG表示が
        # 内部レスポンスで埋まらないようにする)
        if not msg:
            msg = "OK" if success else raw.strip()[:200]
        return AmiResponse(success=success, message=msg, raw=raw)

    async def reload_module(self, module: str) -> AmiResponse:
        """指定モジュールを reload (例: 'res_pjsip.so', 'app_queue.so')。"""
        return await self.action({"Action": "Reload", "Module": module})

    async def reload_all(self) -> AmiResponse:
        """全モジュール reload (重い)。"""
        return await self.action({"Action": "Reload"})

    async def command(self, cli: str) -> AmiResponse:
        """``asterisk -rx`` 相当の CLI コマンドを実行。"""
        return await self.action({"Action": "Command", "Command": cli})

    # --- 内部 ---

    @staticmethod
    def _new_action_id() -> str:
        return secrets.token_hex(8)

    @staticmethod
    def _extract_message(raw: str) -> str:
        for line in raw.splitlines():
            if line.lower().startswith("message:"):
                return line.split(":", 1)[1].strip()
        return ""

    @staticmethod
    def _clean(value: str) -> str:
        """AMI のフィールド値から CR/LF を落とす。

        AMI のプロトコルは 1 行 1 フィールドで、空行がメッセージの
        終端になる。値に改行が混ざると、そこから先が別の Action として
        解釈されてしまう (AMI コマンドインジェクション)。
        現状 AMI へ渡しているのは固定値とモジュール名だけだが、
        将来ユーザー入力を渡したときの事故を防ぐためここで落とす。
        """
        return str(value).replace("\r", " ").replace("\n", " ")

    async def _send(self, fields: dict[str, str]) -> None:
        if self._writer is None:
            raise RuntimeError("not connected")
        body = (
            "".join(f"{self._clean(k)}: {self._clean(v)}\r\n" for k, v in fields.items())
            + "\r\n"
        )
        self._writer.write(body.encode("utf-8"))
        await self._writer.drain()

    async def _read_response_for(self, action_id: str) -> str:
        """指定の ActionID に対応するレスポンスブロックを読む。

        Action と無関係な Event ブロック (FullyBooted, Reload 等) はスキップ。
        ActionID が一致するブロック (Response: ... を含む) を返す。
        """
        if self._reader is None:
            raise RuntimeError("not connected")

        while True:
            block = await self._read_block()
            if not block.strip():
                continue
            # ActionID が一致するブロックなら返す
            if f"ActionID: {action_id}" in block:
                return block
            # それ以外 (非同期 Event 等) はログだけ出して捨てる
            if "Event:" in block:
                lines = block.splitlines()
                log.debug("AMI event ignored: %s", lines[0] if lines else "")
                continue
            # ActionID 無しの Response (旧 Asterisk 互換) も拾う
            if "Response:" in block:
                return block

    async def _read_block(self) -> str:
        """空行で区切られた 1 ブロックを読む。"""
        if self._reader is None:
            raise RuntimeError("not connected")
        chunks: list[bytes] = []
        while True:
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=self.timeout
            )
            if line in (b"\r\n", b"\n"):
                break
            if line == b"":
                break  # 接続切断
            chunks.append(line)
        return b"".join(chunks).decode("utf-8", errors="replace")


async def reload_asterisk_safely() -> dict[str, str]:
    """関連モジュールを順番に reload する。失敗しても例外は出さない。

    AMI の ``Action: Reload`` でモジュール指定するのが最も確実
    (旧 CLI ``pjsip reload`` 方式は Event を取り違えて誤判定していた)。

    対応:
      res_pjsip.so       ... pjsip.conf
      pbx_config.so      ... extensions.conf (dialplan)
      app_queue.so       ... queues.conf
      app_voicemail.so   ... voicemail.conf
      res_musiconhold.so ... musiconhold.conf
      res_parking.so     ... res_parking.conf
      features           ... features.conf (CLI 経由)
    """
    targets: list[tuple[str, str]] = [
        ("pjsip", "res_pjsip.so"),
        ("dialplan", "pbx_config.so"),
        ("queues", "app_queue.so"),
        ("voicemail", "app_voicemail.so"),
        ("musiconhold", "res_musiconhold.so"),
        ("parking", "res_parking.so"),
        # Asterisk 22 の features モジュールは AMI Action: Reload に対して
        # ".so" 付きの名前を渡すと "An unknown error occurred" を返すため、
        # 拡張子なしの "features" で指定する。
        # (CLI でも `module reload features` (.so 無し) が正解)
        ("features", "features"),
    ]
    results: dict[str, str] = {}
    try:
        async with AmiClient() as ami:
            for label, module in targets:
                r = await ami.reload_module(module)
                # AMI Action: Reload で "An unknown error occurred" 等を
                # 返すモジュール (Asterisk 22 の features 等) があるので、
                # 失敗したら CLI 経由 (Action: Command) でフォールバック。
                if not r.success:
                    r2 = await ami.command(f"module reload {module}")
                    if r2.success and (
                        "reloaded successfully" in r2.raw.lower()
                        or "module reloaded" in r2.raw.lower()
                    ):
                        r = r2
                if r.success:
                    results[label] = "OK"
                elif "not reloadable" in r.message.lower() or \
                        "no such" in r.message.lower():
                    results[label] = "skip (モジュールが reload 非対応)"
                else:
                    results[label] = f"NG: {r.message[:200]}"
    except Exception as exc:  # noqa: BLE001
        log.warning("AMI reload failed: %s", exc)
        results["error"] = str(exc)
    return results
