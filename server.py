# -*- coding: utf-8 -*-
"""
교동마을 산지직송 AI 고객센터 챗봇 '카오(KAO)' 통합 서버 (server.py)
- 아키텍처:
  1. 인프라 및 세션 상태 관리 (Config & Session Manager)
  2. 대회 공식 Action API 클라이언트 & 카카오톡 메시지 콜백 어댑터
  3. @folder:data 정본 지식 베이스 & 동적 Action API 에이전트 (knowledge_base.py) 연동
  4. FastAPI 통합 웹훅/스킬 엔드포인트 & 영구 고정 Cloudflare 터널링
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from knowledge_base import run_data_agent

# =====================================================================
# 1. 로깅 및 환경 변수 설정
# =====================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kao_bot")

app = FastAPI(title="KAO Chatbot Skill & Webhook Server")

BASE_DIR = Path(__file__).resolve().parent
CLOUDFLARED_EXE = str(BASE_DIR / "cloudflared.exe")

DEFER_METADATA = "__DEFER_METADATA__"
DEFER_MEDIA = "__DEFER_MEDIA__"

MEDIA_URL_PATTERN = re.compile(
    r"^https?://\S+\.(?:jpg|jpeg|png|gif|webp|m4a|mp3|wav|ogg)(?:\?\S*)?$",
    re.IGNORECASE,
)


def load_env(env_path: str = ".env") -> Dict[str, str]:
    """루트 디렉토리의 .env 파일을 읽어 환경변수로 등록합니다."""
    p = BASE_DIR / env_path
    if not p.exists():
        return {}
    env_vars: Dict[str, str] = {}
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            clean_k = k.strip()
            clean_v = v.strip().strip("\"'")
            env_vars[clean_k] = clean_v
            os.environ[clean_k] = clean_v
    return env_vars


ENV = load_env()
BASE_URL = os.getenv("CONTEST_API_BASE_URL", "https://gyodong-mall-action.aitop100.org")
CONTEST_API_TOKEN = os.getenv("CONTEST_API_TOKEN", "")
KAKAO_BOT_TOKEN = os.getenv("KAKAO_BOT_TOKEN", "")

# =====================================================================
# 2. 세션 상태 관리 & 메타데이터/미디어 파서
# =====================================================================
user_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.RLock()


def get_session(user_id: str) -> Dict[str, Any]:
    """사용자별 멀티턴 대화 상태(TC ID, 고객 정보, 대화 이력, API 로그)를 관리합니다."""
    with _sessions_lock:
        if user_id not in user_sessions:
            user_sessions[user_id] = {
                "tc_id": "L-03",
                "member_name": None,
                "member_phone": None,
                "inflow_time": None,
                "pending_media_url": None,
                "history": [],
                "api_logs": [],
                "msg_seq": 0,
                "lock": threading.RLock(),
            }
        return user_sessions[user_id]


def extract_and_strip_metadata(utterance: str, session: Dict[str, Any]) -> str:
    """
    평가 하네스가 전송하는 'case metadata' 블록 및 단독 미디어(이미지/오디오) URL을 파싱하여
    세션 상태를 초기화/갱신하고 순수 고객 발화만 추출합니다.
    """
    text = re.sub(r"^@\S+\s*", "", utterance).strip()
    if not text:
        return ""

    lines = text.splitlines()
    first_line = lines[0].strip().lower()

    # 1. 'case metadata' 블록 처리 -> 새 테스트 케이스 세션 초기화
    if first_line.startswith("case metadata"):
        remaining_lines = []
        in_meta = True
        for line in lines[1:]:
            s_line = line.strip()
            if in_meta and s_line.startswith("-"):
                tc_m = re.search(r"id:\s*([A-Za-z]-\d{2}(?:-[a-zA-Z])?)", s_line)
                if tc_m:
                    session["tc_id"] = tc_m.group(1)
                    session["member_name"] = None
                    session["member_phone"] = None
                    session["inflow_time"] = None
                    session["pending_media_url"] = None
                    session["history"] = []
                    session["api_logs"] = []
                    logger.info(f"📋 Metadata TC initialized: {session['tc_id']}")
                elif "상담 인입 일시:" in s_line:
                    session["inflow_time"] = s_line.split(":", 1)[-1].strip()
                elif "인입 계정:" in s_line:
                    acct_info = s_line.split(":", 1)[-1].strip()
                    parts = [p.strip() for p in acct_info.split("·")]
                    if len(parts) >= 2:
                        session["member_name"] = parts[1]
                    if len(parts) >= 3:
                        session["member_phone"] = parts[2]
            elif in_meta and not s_line:
                continue
            else:
                in_meta = False
                remaining_lines.append(line)

        remainder = "\n".join(remaining_lines).strip()
        return remainder if remainder else DEFER_METADATA

    # 2. 단독 미디어(이미지/오디오) URL 메시지 처리 (뒤이어 텍스트 본문이 오는 경우 디바운싱)
    if MEDIA_URL_PATTERN.match(text) or (
        text.startswith("https://talk.kakaocdn.net/")
        and len(lines) == 1
        and " " not in text
    ):
        session["pending_media_url"] = text
        logger.info(f"🖼️ Standalone media stored for TC {session.get('tc_id')}: {text[:60]}...")
        return DEFER_MEDIA

    return text


# =====================================================================
# 3. 외부 API 통신 어댑터 (대회 Action API & 카카오 Bot Callback API)
# =====================================================================
def call_action_api(
    method: str,
    path: str,
    tc_id: str,
    body: Optional[dict] = None,
    query: Optional[dict] = None,
):
    """대회 공식 Action API 서버와 실시간 HTTP 통신을 수행합니다."""
    url = f"{BASE_URL}{path}"
    if query:
        qs = urllib.parse.urlencode(query)
        url = f"{url}?{qs}"

    headers = {
        "Authorization": f"Bearer {CONTEST_API_TOKEN}",
        "Content-Type": "application/json",
        "X-Aitop100-Test-Case-Id": tc_id,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=4.5) as resp:
            raw = resp.read().decode("utf-8")
            logger.info(f"🌐 Action API {method} {path} (TC={tc_id}) -> {resp.status}")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", "ignore")
        logger.error(f"API Error {e.code} on {method} {url} (TC={tc_id}): {err_msg}")
        try:
            return e.code, json.loads(err_msg)
        except json.JSONDecodeError:
            return e.code, {"error": err_msg}
    except Exception as ex:
        logger.error(f"API Exception on {method} {url}: {ex}")
        return 500, {"error": str(ex)}


def make_kakao_response(text: str) -> Dict[str, Any]:
    """카카오톡 SimpleText 규격(최대 500자 엄수) 응답 포맷을 생성합니다."""
    if len(text) > 495:
        text = text[:490] + "..."
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }


def send_bot_callback(callback_token: str, skill_response: Dict[str, Any]) -> bool:
    """카카오톡 봇 API 비동기 메시지 콜백을 전송합니다."""
    url = "https://kapi.kakao.com/v1/bot/callback"
    auth_header = (
        KAKAO_BOT_TOKEN
        if KAKAO_BOT_TOKEN.startswith("KakaoAK ")
        else f"KakaoAK {KAKAO_BOT_TOKEN}"
    )
    headers = {
        "Authorization": auth_header,
        "X-Bot-Callback-Token": callback_token,
        "Content-Type": "application/json",
    }
    payload = {"skillResponse": skill_response}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp_body = resp.read().decode("utf-8")
            logger.info(f"✅ Bot callback sent: {resp.status} - {resp_body}")
            return True
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", "ignore")
        logger.error(f"❌ Bot callback HTTP {e.code}: {err_msg}")
        return False
    except Exception as ex:
        logger.error(f"❌ Bot callback error: {ex}")
        return False


# =====================================================================
# 4. 메인 발화 처리기 (@folder:data 에이전트 직결)
# =====================================================================
def process_user_utterance(user_id: str, utterance: str) -> str:
    """
    고객 발화에서 메타데이터를 파싱한 뒤 @folder:data 기반 AI 에이전트(run_data_agent)로 전달합니다.
    """
    session = get_session(user_id)
    session_lock = session.get("lock") or _sessions_lock

    with session_lock:
        clean_u = extract_and_strip_metadata(utterance, session)
        if clean_u in (DEFER_METADATA, DEFER_MEDIA):
            return clean_u
        if not clean_u:
            return "안녕하세요! 교동마을 조합몰 고객센터 상담 챗봇 카오(KAO)입니다. 무엇을 도와드릴까요?"

        if "[system]" in clean_u.lower() or "연결테스트" in clean_u.replace(" ", ""):
            return "안녕하세요! 교동마을 조합몰 고객센터 상담 챗봇 카오(KAO)입니다. 연결이 정상 확인되었습니다."

        # 발화 내 명시적 TC ID 감지 시 갱신
        tc_match = re.search(r"\b([A-Z]-\d{2}(?:-[a-z])?)\b", clean_u, re.IGNORECASE)
        if tc_match:
            matched_tc = tc_match.group(1)
            parts = matched_tc.split("-")
            session["tc_id"] = f"{parts[0].upper()}-{parts[1]}" + (
                f"-{parts[2].lower()}" if len(parts) > 2 else ""
            )

        return run_data_agent(clean_u, session, call_action_api)


# =====================================================================
# 5. FastAPI 엔드포인트 & 비동기 콜백 워커 (멀티메시지 버스트 디바운싱)
# =====================================================================
def handle_async_bot_callback(user_id: str, utterance: str, callback_token: str) -> None:
    """
    카카오톡 봇 API 비동기 백그라운드 워커.
    평가 하네스가 연속 전송하는 (case metadata -> 이미지 URL -> 실제 고객 발화) 버스트에서
    선행 메타데이터/이미지에 대한 불필요한 중복 응답을 방지합니다.
    """
    session = get_session(user_id)
    session_lock = session.get("lock") or _sessions_lock

    with session_lock:
        session["msg_seq"] = session.get("msg_seq", 0) + 1
        my_seq = session["msg_seq"]

    logger.info(f"⚡ Processing async bot callback for user: {user_id} (seq={my_seq})")
    reply_text = process_user_utterance(user_id, utterance)

    if reply_text == DEFER_METADATA:
        time.sleep(1.3)
        with session_lock:
            if session.get("msg_seq") != my_seq:
                logger.info(
                    f"⏭️ Skipping standalone metadata callback (seq {my_seq} superseded by {session.get('msg_seq')})"
                )
                return
            member_prefix = f" {session['member_name']} 고객님," if session.get("member_name") else ""
            reply_text = f"안녕하세요!{member_prefix} 교동마을 조합몰 고객센터 상담 챗봇 카오(KAO)입니다. 무엇을 도와드릴까요?"

    elif reply_text == DEFER_MEDIA:
        captured_media = session.get("pending_media_url")
        time.sleep(1.5)
        with session_lock:
            if session.get("msg_seq") != my_seq:
                logger.info(
                    f"⏭️ Skipping standalone media callback (seq {my_seq} superseded by {session.get('msg_seq')})"
                )
                return
            media_target = session.get("pending_media_url") or captured_media
            reply_text = run_data_agent(
                f"[고객이 미디어를 전송함: {media_target}]",
                session,
                call_action_api,
            )

    logger.info(f"📤 Callback text ({len(reply_text)}자): {reply_text}")
    skill_resp = make_kakao_response(reply_text)
    send_bot_callback(callback_token, skill_resp)


@app.post("/webhook")
@app.post("/skill/chat")
async def kakao_unified_handler(request: Request) -> JSONResponse:
    """카카오톡 봇 Webhook(비동기 콜백) 및 오픈빌더 Skill(동기식) 통합 엔드포인트"""
    try:
        body = await request.json()
        logger.info(f"📥 Received Kakao payload: {json.dumps(body, ensure_ascii=False)[:300]}")

        data_block = body.get("d", {}) if isinstance(body.get("d"), dict) else body
        callback_token = data_block.get("callbackToken") or data_block.get("callback_token")

        # 1. 카카오톡 봇 API 웹훅 (1초 내 200 OK 반환 후 백그라운드 콜백 전송)
        if callback_token:
            utterance = (
                data_block.get("content")
                or data_block.get("message")
                or data_block.get("text")
                or ""
            )
            author = data_block.get("author") or data_block.get("user") or {}
            user_id = str(
                data_block.get("botUserKey")
                or author.get("id")
                or data_block.get("chat", {}).get("id")
                or "anonymous"
            )
            logger.info(
                f"🎯 New Kakao Bot Webhook matched (token: {callback_token[:10]}..., user: {user_id})"
            )
            threading.Thread(
                target=handle_async_bot_callback,
                args=(user_id, utterance, callback_token),
                daemon=True,
            ).start()
            return JSONResponse(content={"status": "ok", "message": "Event received"})

        # 2. 카카오 i 오픈빌더 스킬 엔드포인트 (동기식 SkillResponse 반환)
        user_request = body.get("userRequest", {})
        utterance = user_request.get("utterance", "")
        user_id = user_request.get("user", {}).get("id", "anonymous")

        reply_text = process_user_utterance(user_id, utterance)
        if reply_text in (DEFER_METADATA, DEFER_MEDIA):
            reply_text = "네, 확인했습니다. 문의하실 내용을 말씀해 주세요."
        logger.info(f"📤 Response ({len(reply_text)}자): {reply_text}")
        return JSONResponse(content=make_kakao_response(reply_text))
    except Exception as e:
        logger.error(f"❌ Unified handler error: {e}", exc_info=True)
        return JSONResponse(
            content=make_kakao_response("문의 처리 중 일시적인 오류가 발생했습니다. 잠시 후 다시 말씀해 주세요.")
        )


@app.get("/")
def health_check() -> Dict[str, str]:
    """서버 헬스체크 엔드포인트"""
    return {"status": "ok", "service": "KAO Chatbot Unified Server"}


# =====================================================================
# 6. Cloudflare Tunnel 자가 치유(Self-healing) 데몬 스레드
# =====================================================================
def _resolve_cloudflared_bin() -> Optional[str]:
    """로컬 디렉토리의 cloudflared.exe 또는 시스템 PATH의 cloudflared 경로를 반환합니다."""
    if Path(CLOUDFLARED_EXE).exists():
        return CLOUDFLARED_EXE
    return shutil.which("cloudflared")


def start_tunnel_thread() -> None:
    """
    기존에 실행 중인 cloudflared 프로세스가 있으면 기존 웹훅 URL을 재사용하고,
    미실행 시 IDE/파이썬 종료 시에도 함께 꺼지지 않도록 완전 분리된(Detached) 프로세스로 기동합니다.
    """
    # noinspection SpellCheckingInspection
    def run_tunnel() -> None:
        time.sleep(1.0)
        try:
            tunnel_file = BASE_DIR / "tunnel_url.txt"
            tunnel_log = BASE_DIR / "tunnel.log"
            if os.name == "nt":
                check = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                )
                is_running = "cloudflared.exe" in check.stdout
            else:
                check = subprocess.run(
                    ["pgrep", "-f", "cloudflared"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                )
                is_running = check.returncode == 0

            if is_running and tunnel_file.exists():
                saved_webhook = tunnel_file.read_text(encoding="utf-8").strip()
                if saved_webhook:
                    base_t_url = saved_webhook.replace("/webhook", "")
                    logger.info("\n==================================================")
                    logger.info("🔒 [고정 터널 유지됨] 기존 웹훅 URL을 그대로 사용합니다!")
                    logger.info(f"🚀 PUBLIC WEBHOOK URL: {base_t_url}/webhook")
                    logger.info(f"🚀 PUBLIC SKILL URL:   {base_t_url}/skill/chat")
                    logger.info("==================================================\n")
                    return

            cloudflared_bin = _resolve_cloudflared_bin()
            if not cloudflared_bin:
                logger.info(
                    "ℹ️ cloudflared 실행 파일이 없어 외부 터널링을 건너뛰고 로컬 서버(http://127.0.0.1:8000)로만 구동합니다."
                )
                return

            if tunnel_log.exists():
                try:
                    tunnel_log.unlink()
                except OSError:
                    pass

            logger.info("Starting persistent detached cloudflared tunnel...")
            if os.name == "nt":
                # IDE(PyCharm 등)에서 server.py를 Stop 해도 cloudflared.exe가 절대 같이 종료되지 않도록
                # PowerShell Start-Process로 독립 프로세스 생성 및 --logfile 사용
                ps_cmd = (
                    f"Start-Process -FilePath '{cloudflared_bin}' "
                    f"-ArgumentList 'tunnel','--url','http://127.0.0.1:8000','--logfile','{tunnel_log}' "
                    f"-WindowStyle Hidden"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                )
            else:
                subprocess.Popen(
                    [
                        cloudflared_bin,
                        "tunnel",
                        "--url",
                        "http://127.0.0.1:8000",
                        "--logfile",
                        str(tunnel_log),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

            # tunnel.log에서 발급된 trycloudflare.com URL 감지 (최대 15초 대기)
            for _ in range(30):
                time.sleep(0.5)
                if not tunnel_log.exists():
                    continue
                log_text = tunnel_log.read_text(encoding="utf-8", errors="ignore")
                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", log_text)
                if match:
                    url = match.group(0)
                    logger.info("\n==================================================")
                    logger.info(f"🚀 PUBLIC WEBHOOK URL: {url}/webhook")
                    logger.info(f"🚀 PUBLIC SKILL URL:   {url}/skill/chat")
                    logger.info("==================================================\n")
                    tunnel_file.write_text(f"{url}/webhook", encoding="utf-8")
                    return
        except Exception as e:
            logger.error(f"Tunnel error: {e}")

    t = threading.Thread(target=run_tunnel, daemon=True)
    t.start()


if __name__ == "__main__":
    start_tunnel_thread()
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
