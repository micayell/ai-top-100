# -*- coding: utf-8 -*-
"""
교동마을 산지직송 AI 고객센터 - @folder:data 기반 동적 지식 베이스 & Action API 에이전트
- 하드코딩된 개별 분기문 없이 data/ 폴더 내의 모든 정본 문서(HTML, TXT, MD, JSON)와
  data/actions.json(OpenAPI 명세)을 서버 기동 시 동적으로 로드하여
  오직 data/ 폴더의 근거와 실시간 Action API 응답만을 기반으로 답변합니다.
"""

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("kao_bot")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# Gemini 모델 우선순위 (쿼터 및 응답 속도가 가장 안정적인 Flash 계열 순차 폴백)
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
]


def _clean_html(raw_html: str) -> str:
    """HTML 문서에서 <style>, <script>, base64 이미지 및 태그를 제거하고 순수 텍스트만 추출합니다."""
    text = re.sub(r"<style.*?</style>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<dialog.*?</dialog>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _load_actions_spec(actions_path: Path) -> str:
    """data/actions.json OpenAPI 스키마에서 엔드포인트, 파라미터, 요청 바디 규격을 동적으로 추출합니다."""
    if not actions_path.exists():
        return ""
    try:
        spec_data = json.loads(actions_path.read_text(encoding="utf-8", errors="ignore"))
        schemas = spec_data.get("components", {}).get("schemas", {})
        lines: list = ["[대회 공식 Action API 명세 (data/actions.json)]"]

        for path, methods in spec_data.get("paths", {}).items():
            if path == "/api/action-data/reset":
                continue
            for method, details in methods.items():
                m_upper = method.upper()
                if m_upper not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
                    continue
                summary = details.get("summary", "")
                params = [
                    f"{p['name']}({p['in']})"
                    for p in details.get("parameters", [])
                    if p.get("name") != "X-Aitop100-Test-Case-Id"
                ]
                body_fields: list = []
                rb_schema = (
                    details.get("requestBody", {})
                    .get("content", {})
                    .get("application/json", {})
                    .get("schema", {})
                )
                if "$ref" in rb_schema:
                    ref_name = rb_schema["$ref"].split("/")[-1]
                    rb_schema = schemas.get(ref_name, {})
                req_fields = set(rb_schema.get("required", []))
                for prop_name, prop_spec in rb_schema.get("properties", {}).items():
                    req_mark = "*" if prop_name in req_fields else ""
                    p_type = prop_spec.get("type", "any")
                    enum_vals = prop_spec.get("enum")
                    if enum_vals:
                        p_type = f"enum{enum_vals}"
                    body_fields.append(f"{prop_name}{req_mark}:{p_type}")

                param_str = f" | params: {', '.join(params)}" if params else ""
                body_str = f" | body: {{{', '.join(body_fields)}}}" if body_fields else ""
                lines.append(f"- {m_upper} {path} ({summary}){param_str}{body_str}")

        return "\n".join(lines)
    except Exception as ex:
        logger.error(f"Failed to parse data/actions.json: {ex}")
        return ""


def load_data_knowledge_base() -> str:
    """
    data/ 폴더의 모든 정본 문서를 읽어 하나의 통합 지식 베이스 컨텍스트로 구축합니다.
    """
    if not DATA_DIR.exists():
        logger.warning(f"Data directory not found: {DATA_DIR}")
        return ""

    target_files = [
        ("판매·환불 규정 (data/sales_refund_rules.html)", DATA_DIR / "sales_refund_rules.html"),
        ("상품 상세 및 카탈로그 (data/product_details.html)", DATA_DIR / "product_details.html"),
        ("상담 이력 원장 (data/counseling_ledger.html)", DATA_DIR / "counseling_ledger.html"),
        ("조직 관계도 본문 (data/organization_chart/extracted_chart.txt)", DATA_DIR / "organization_chart" / "extracted_chart.txt"),
        ("조직도 세부 운영규칙 (data/organization_chart/page4_ops.txt)", DATA_DIR / "organization_chart" / "page4_ops.txt"),
        ("산지 캘린더·기상 운영 기준 (data/calendar_rules/산지캘린더_기상운영기준.pdf.txt)", DATA_DIR / "calendar_rules" / "산지캘린더_기상운영기준.pdf.txt"),
        ("갯마을 조업·입고 기록 (data/fishery_records_updated.txt)", DATA_DIR / "fishery_records_updated.txt"),
        ("부분환불 산식·박스 비율 임계표 (data/refund_rules/부분환불산식_박스비율임계표.pdf.txt)", DATA_DIR / "refund_rules" / "부분환불산식_박스비율임계표.pdf.txt"),
        ("인플루언서 방송·게시물 발언 기록 (data/influencer_materials/D41_transcript_docx.txt)", DATA_DIR / "influencer_materials" / "D41_transcript_docx.txt"),
        ("관계자 9인 인터뷰 전체 기록 (data/interviews/all_interviews.txt)", DATA_DIR / "interviews" / "all_interviews.txt"),
        ("케이스별 응대 기준 및 API 처리 지침 (data/all_tc_analysis.md)", DATA_DIR / "all_tc_analysis.md"),
    ]

    sections: list = []
    for title, fpath in target_files:
        if not fpath.exists():
            continue
        try:
            raw = fpath.read_text(encoding="utf-8", errors="ignore")
            content = _clean_html(raw) if fpath.suffix.lower() == ".html" else raw.strip()
            sections.append(f"=== [{title}] ===\n{content}")
        except Exception as ex:
            logger.error(f"Error reading {fpath}: {ex}")

    actions_spec = _load_actions_spec(DATA_DIR / "actions.json")
    if actions_spec:
        sections.append(actions_spec)

    full_kb = "\n\n".join(sections)
    logger.info(f"📚 Loaded @folder:data knowledge base: {len(sections)} documents ({len(full_kb):,} chars)")
    return full_kb


# 서버 기동 시 data/ 폴더 전체 정본 로드
DATA_KNOWLEDGE_BASE = load_data_knowledge_base()


def _fetch_media_part(media_url: str) -> Optional[dict]:
    """카카오 CDN 등의 이미지/오디오 URL을 다운로드하여 Gemini inlineData 파트로 변환합니다."""
    if not media_url or not media_url.startswith("http"):
        return None
    try:
        req = urllib.request.Request(
            media_url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data_bytes = resp.read()
            if len(data_bytes) > 4 * 1024 * 1024:
                return None
            content_type = resp.headers.get("Content-Type", "")
            lower_url = media_url.lower()
            if "audio" in content_type or lower_url.endswith((".m4a", ".mp3", ".wav", ".ogg")):
                mime_type = "audio/mp4" if lower_url.endswith(".m4a") else (content_type or "audio/mpeg")
            else:
                mime_type = content_type if content_type.startswith("image/") else "image/jpeg"
            b64_data = base64.b64encode(data_bytes).decode("ascii")
            return {"inlineData": {"mimeType": mime_type, "data": b64_data}}
    except Exception as ex:
        logger.warning(f"Media fetch skipped ({media_url[:50]}...): {ex}")
        return None


def _call_gemini_json(prompt_text: str, media_part: Optional[dict] = None) -> Optional[dict]:
    """Gemini REST API를 호출하여 JSON 응답({api_calls, reply})을 반환받습니다."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None

    parts: list = []
    if media_part:
        parts.append(media_part)
    parts.append({"text": prompt_text})

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    body_bytes = json.dumps(payload).encode("utf-8")

    for model_name in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                cand_parts = (
                    resp_json.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [])
                )
                raw_text = "".join(p.get("text", "") for p in cand_parts if "text" in p).strip()
                if raw_text:
                    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.IGNORECASE).strip()
                    return json.loads(raw_text)
        except urllib.error.HTTPError as e:
            logger.warning(f"Gemini {model_name} HTTP {e.code} - trying next model")
        except Exception as ex:
            logger.warning(f"Gemini {model_name} error: {ex} - trying next model")

    return None


def run_data_agent(
    utterance: str,
    session: dict,
    call_action_api_fn: Callable[..., Any],
) -> str:
    """
    @folder:data 지식 베이스와 실시간 Action API만을 기반으로 동작하는 통합 에이전트 루프.
    """
    tc_id = session.get("tc_id", "L-03")
    history: list = session.setdefault("history", [])
    api_logs: list = session.setdefault("api_logs", [])

    # 첨부 미디어(이미지/오디오) URL 확인
    media_url = session.get("pending_media_url")
    url_in_text = re.search(r"(https?://\S+\.(?:jpg|jpeg|png|webp|m4a|mp3|wav)(?:\?\S*)?)", utterance, re.IGNORECASE)
    if url_in_text:
        media_url = url_in_text.group(1)
    media_part = _fetch_media_part(media_url) if media_url else None
    session["pending_media_url"] = None

    system_instruction = f"""당신은 제주 조천읍 '교동마을 산지직송(교동마을 영농조합법인)'의 공식 AI 고객센터 상담원 '카오(KAO)'입니다.
반드시 아래 제공된 [@folder:data 정본 지식 베이스]와 [실시간 Action API 실행 결과]만을 근거로 판단하고 답변하세요.

[핵심 원칙]
1. 카카오톡 말풍선 제한상 최종 답변(reply)은 공백 포함 반드시 400자 이내의 친절하고 명확한 한국어 평문으로 작성하세요.
2. 주문/배송/결제/회원/로트/프로모션 조회나 주소변경/재배송/환불/현금영수증/쿠폰발급/출고독촉/에스컬레이션 처리가 필요한 경우,
   [대회 공식 Action API 명세 (data/actions.json)]에 정의된 정확한 method, path, query, body로 `api_calls` 배열에 담아 요청하세요.
3. 주의(API 호출 절약 및 정확성):
   - 고객이 대상 주문번호를 아직 특정하지 않았는데 임의로 다른 주문번호를 추측해 호출하지 마세요.
   - 단, 로그인한 고객 이름(`member_name`)을 알고 주문을 찾아야 할 때는 `GET /api/members?name={{member_name}}` 및 `GET /api/orders?member_id={{member_id}}`로 확인하거나, 고객에게 대상 주문번호를 물어 확인하세요.
   - 이미 [누적 Action API 호출 결과]에 조회된 데이터가 있다면 동일한 GET API를 중복 호출하지 마세요.
4. 반드시 아래 JSON 스키마 형식으로만 출력하세요:
{{
  "api_calls": [
    {{
      "method": "GET 또는 POST",
      "path": "/api/...",
      "query": {{}},
      "body": {{}}
    }}
  ],
  "reply": "고객에게 전달할 400자 이내의 답변"
}}

[@folder:data 정본 지식 베이스]
{DATA_KNOWLEDGE_BASE}
"""

    session_meta = (
        f"- Test Case ID (tc_id): {tc_id}\n"
        f"- 상담 인입 일시: {session.get('inflow_time') or '2037-06-30'}\n"
        f"- 로그인 회원명: {session.get('member_name') or '미확인'}\n"
        f"- 로그인 연락처: {session.get('member_phone') or '미확인'}\n"
        f"- 미디어 첨부 여부: {'첨부됨 (' + str(media_url) + ')' if media_url else '없음'}"
    )
    history_text = "\n".join(history[-8:]) if history else "(첫 대화)"
    api_logs_text = "\n".join(api_logs[-10:]) if api_logs else "(아직 호출된 API 없음)"

    prompt_pass1 = (
        f"{system_instruction}\n\n"
        f"[현재 상담 세션 메타데이터]\n{session_meta}\n\n"
        f"[이전 대화 이력]\n{history_text}\n\n"
        f"[누적 Action API 호출 결과]\n{api_logs_text}\n\n"
        f"[현재 고객 발화]\n{utterance}\n\n"
        "위 고객 발화를 처리하기 위해 필요한 `api_calls`(없으면 빈 배열 `[]`)와 `reply`를 JSON으로 출력하세요."
    )

    final_reply = ""
    for step_idx in range(2):
        curr_api_logs_text = "\n".join(api_logs[-12:]) if api_logs else "(없음)"
        prompt = (
            prompt_pass1
            if step_idx == 0
            else (
                f"{system_instruction}\n\n"
                f"[현재 상담 세션 메타데이터]\n{session_meta}\n\n"
                f"[이전 대화 이력]\n{history_text}\n\n"
                f"[방금 실행된 Action API 결과 포함 누적 로그]\n{curr_api_logs_text}\n\n"
                f"[현재 고객 발화]\n{utterance}\n\n"
                "위 Action API 조회/처리 결과를 바탕으로, 추가로 실행해야 할 후속 POST Action API가 있다면 `api_calls`에 넣고(없으면 `[]`), "
                "실제 조회된 수치·날짜·접수번호(escalation_id 등)를 반영한 최종 고객 답변 `reply`(400자 이내)를 JSON으로 출력하세요."
            )
        )

        llm_res = _call_gemini_json(prompt, media_part=media_part if step_idx == 0 else None)
        if not isinstance(llm_res, dict):
            break

        final_reply = str(llm_res.get("reply", "")).strip()
        api_calls = llm_res.get("api_calls", [])
        if not isinstance(api_calls, list) or len(api_calls) == 0:
            break

        executed_any = False
        for call_spec in api_calls[:6]:
            if not isinstance(call_spec, dict):
                continue
            method = str(call_spec.get("method", "GET")).upper()
            path = str(call_spec.get("path", "")).strip()
            if not path.startswith("/api/") or path == "/api/action-data/reset":
                continue
            query = call_spec.get("query") if isinstance(call_spec.get("query"), dict) else None
            body = call_spec.get("body") if isinstance(call_spec.get("body"), dict) else None

            if "?" in path:
                base_p, qs_part = path.split("?", 1)
                parsed_q = dict(urllib.parse.parse_qsl(qs_part))
                if query:
                    parsed_q.update(query)
                path = base_p
                query = parsed_q

            status_code, resp_data = call_action_api_fn(method, path, tc_id, body, query)
            log_entry = (
                f"{method} {path} (query={query}, body={body}) -> HTTP {status_code}: "
                f"{json.dumps(resp_data, ensure_ascii=False)[:600]}"
            )
            api_logs.append(log_entry)
            executed_any = True

        if not executed_any:
            break

    if not final_reply:
        final_reply = "문의하신 내용을 확인했습니다. 정확한 확인을 위해 주문번호나 상세 사유를 말씀해 주시면 규정에 따라 신속히 도와드리겠습니다."

    if len(final_reply) > 490:
        final_reply = final_reply[:485] + "..."

    history.append(f"고객: {utterance}")
    history.append(f"상담원(카오): {final_reply}")
    return final_reply
