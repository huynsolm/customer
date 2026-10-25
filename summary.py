"""부족 품목 요약. 숫자는 inventory.shortage_report가 계산하고, Claude는 그 결과를 문장으로만 바꾼다.

Claude 호출은 선택 사항이다. anthropic 패키지나 인증 정보가 없거나, 호출이 실패하거나,
답에 입력에 없는 숫자가 섞이면 규칙 기반 문장(template)으로 대신한다.
"""
import json
import re

import inventory as inv

MODEL = "claude-opus-5-5"
SYSTEM = (
    "너는 작은 카페 점장에게 마감 후 재고 상황을 알려주는 도우미다. "
    "입력 JSON의 숫자만 사용하고, 새로운 숫자를 계산하거나 추측하지 마라. "
    "short가 true인 품목을 먼저, 권장 발주량(suggested_qty)과 단위를 함께 말하라. "
    "days_left가 있는 품목은 '약 N일 분'처럼 버틸 수 있는 기간을 덧붙여라. "
    "한국어로 3문장 이내, 목록이나 마크다운 없이 평문으로 써라."
)


def template_summary(rows):
    short = [r for r in rows if r["short"]]
    if not short:
        return "발주 기준 이하인 품목이 없습니다."
    parts = []
    for r in short:
        s = "%s %d%s 주문 권장(현재 %d%s" % (r["name"], r["suggested_qty"], r["unit"], r["stock"], r["unit"])
        if r["days_left"] is not None:
            s += ", 약 %s일 분" % _fmt(r["days_left"])
        if r["incoming"]:
            s += ", 입고 예정 %d%s" % (r["incoming"], r["unit"])
        parts.append(s + ")")
    return "부족 품목 %d개: %s." % (len(short), ", ".join(parts))


def _fmt(x):
    return str(int(x)) if float(x).is_integer() else str(x)


def _allowed_numbers(rows):
    nums = {len(rows), len([r for r in rows if r["short"]]), inv.USAGE_DAYS, inv.COVER_DAYS}
    for r in rows:
        nums.update(v for v in r.values() if isinstance(v, (int, float)) and not isinstance(v, bool))
    return {float(n) for n in nums}


def numbers_grounded(text, rows):
    """답에 나온 숫자가 모두 입력에 있는지 확인한다(AI가 수량을 지어내지 못하게)."""
    allowed = _allowed_numbers(rows)
    return all(float(n) in allowed for n in re.findall(r"\d+(?:\.\d+)?", text))


def ai_summary(rows, client=None):
    """(문장, 출처, 비고)를 돌려준다. 출처는 'claude' 또는 'template'."""
    fallback = template_summary(rows)
    try:
        import anthropic
    except ImportError:
        return fallback, "template", "anthropic 패키지가 설치되지 않음"
    try:
        client = client or anthropic.Anthropic()
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(rows, ensure_ascii=False)}],
        )
    except anthropic.AuthenticationError:
        return fallback, "template", "Claude 인증 정보가 없거나 잘못됨"
    except anthropic.RateLimitError:
        return fallback, "template", "Claude 사용량 한도 초과"
    except anthropic.APIStatusError as e:
        return fallback, "template", "Claude API 오류(%s)" % e.status_code
    except anthropic.APIConnectionError:
        return fallback, "template", "Claude에 연결할 수 없음"
    except anthropic.AnthropicError as e:
        return fallback, "template", "Claude 클라이언트 오류: %s" % e
    if resp.stop_reason == "refusal":
        return fallback, "template", "Claude가 요청을 거절함"
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        return fallback, "template", "Claude 응답이 비어 있음"
    if not numbers_grounded(text, rows):
        return fallback, "template", "Claude 응답에 입력에 없는 숫자가 있어 사용하지 않음"
    return text, "claude", ""
