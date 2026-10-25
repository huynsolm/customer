"""summary.py 테스트. 실제 Claude API는 호출하지 않고 가짜 클라이언트를 쓴다."""
import sys
import types
import unittest
from unittest import mock

import summary

ROWS = [
    {"item_id": 1, "name": "우유", "unit": "개", "stock": 4, "incoming": 0, "reorder_point": 5,
     "daily_use": 3.7, "days_left": 1.1, "short": True, "suggested_qty": 27},
    {"item_id": 2, "name": "원두", "unit": "kg", "stock": 4, "incoming": 0, "reorder_point": 2,
     "daily_use": 0, "days_left": None, "short": False, "suggested_qty": 0},
]


def fake_anthropic():
    """anthropic 패키지 흉내. 예외 클래스 계층만 실제와 같게 둔다."""
    m = types.ModuleType("anthropic")

    class AnthropicError(Exception): pass
    class APIConnectionError(AnthropicError): pass
    class APIStatusError(AnthropicError):
        status_code = 500
    class AuthenticationError(APIStatusError): pass
    class RateLimitError(APIStatusError): pass
    for c in (AnthropicError, APIConnectionError, APIStatusError, AuthenticationError, RateLimitError):
        setattr(m, c.__name__, c)
    return m


def client_returning(text, stop_reason="end_turn"):
    resp = types.SimpleNamespace(stop_reason=stop_reason,
                                 content=[types.SimpleNamespace(type="text", text=text)])
    create = mock.Mock(return_value=resp)
    return types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create))), create


class Summary(unittest.TestCase):
    def setUp(self):
        self.mod = fake_anthropic()
        patcher = mock.patch.dict(sys.modules, {"anthropic": self.mod})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_template(self):
        self.assertEqual(summary.template_summary(ROWS), "부족 품목 1개: 우유 27개 주문 권장(현재 4개, 약 1.1일 분).")
        self.assertEqual(summary.template_summary(ROWS[1:]), "발주 기준 이하인 품목이 없습니다.")

    def test_server_fills_numbers_and_units(self):
        client, create = client_returning("[[1.suggested_qty]] 주문이 가장 급합니다([[1.days_left]]).")
        text, source, _ = summary.ai_summary(ROWS, client)
        self.assertEqual((text, source), ("우유 27개 주문이 가장 급합니다(우유 약 1.1일 분).", "claude"))
        kw = create.call_args.kwargs
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["fallbacks"], "default")

    def test_rejects_numbers_written_by_model(self):
        """2차 검수 #6: 전에는 아래 응답이 모두 통과했다."""
        for bad in ["우유 1~2일 분입니다. [[1.suggested_qty]]",
                    "우유 1,000개 주문 권장 [[1.suggested_qty]]",
                    "우유 백 개 주문 권장 [[1.suggested_qty]]",
                    "[[1.suggested_qty]] 그리고 두 병 더",
                    "우유 27kg 주문 권장",
                    "우유 -27개 주문 권장",
                    "[[9.suggested_qty]] 주문",                      # 없는 품목
                    "[[2.days_left]] [[1.suggested_qty]]",           # 값이 없는 필드
                    "[[1.price]] [[1.suggested_qty]]",               # 없는 필드
                    "원두가 조금 부족합니다."]:                       # 부족 품목 권장량 누락
            client, _ = client_returning(bad)
            text, source, note = summary.ai_summary(ROWS, client)
            self.assertEqual(source, "template", bad)
            self.assertEqual(text, summary.template_summary(ROWS))

    def test_no_shortage_skips_claude(self):
        client, create = client_returning("x")
        self.assertEqual(summary.ai_summary(ROWS[1:], client)[1], "template")
        create.assert_not_called()

    def test_refusal_falls_back(self):
        client, _ = client_returning("", stop_reason="refusal")
        self.assertEqual(summary.ai_summary(ROWS, client)[1], "template")

    def test_api_error_falls_back(self):
        client, create = client_returning("")
        create.side_effect = self.mod.APIConnectionError()
        text, source, note = summary.ai_summary(ROWS, client)
        self.assertEqual((source, note), ("template", "Claude에 연결할 수 없음"))

    def test_missing_package_falls_back(self):
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            self.assertEqual(summary.ai_summary(ROWS)[1:], ("template", "anthropic 패키지가 설치되지 않음"))


if __name__ == "__main__":
    unittest.main()
