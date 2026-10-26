// 화면과 무관한 요청·입력 처리. index.html과 test_client.mjs(Node 테스트)가 함께 쓴다.

// 숫자 입력은 전체가 정수일 때만 받는다(parseInt는 1.9→1, 2abc→2로 잘라 버린다).
function intOf(value, min, label) {
  const s = String(value ?? '').trim();
  if (!/^\d+$/.test(s) || +s < min || +s > 1000000) throw new Error(`${label}은 ${min} 이상 1000000 이하의 정수로 입력하세요`);
  return +s;
}

// 요청 키 관리. 결과를 모르는 요청(응답 유실·서버 오류)은 키와 본문을 남겨, 같은 내용을 다시 보내면
// 같은 키를 써서 서버가 한 번만 반영하게 한다. 처리 중인 같은 요청을 다시 누르는 경우(더블클릭)도 같다.
// 메모리가 기본 상태이고 storage(sessionStorage)는 새로고침 대비 보존용이다. storage를 쓸 수 없어도
// 메모리로는 계속 막고, persisted()가 false가 되어 화면이 "새로고침하면 복구할 수 없음"을 알린다.
function createSender({ fetch, storage, uid, actor = () => '', onChange = () => {} }) {
  const NAME = 'pendingRequests';
  let pending = {};
  let persisted = true;
  const inflight = new Map();  // 응답을 기다리는 중인 요청(sig → 개수). 재전송 안내에서는 뺀다.
  try { pending = JSON.parse(storage.getItem(NAME)) || {}; } catch { persisted = false; }

  function save() {
    try { storage.setItem(NAME, JSON.stringify(pending)); persisted = true; } catch { persisted = false; }
  }
  function track(sig, d) {
    const n = (inflight.get(sig) || 0) + d;
    n > 0 ? inflight.set(sig, n) : inflight.delete(sig);
  }

  async function send(path, body) {
    const who = actor();  // 작업자가 없으면 여기서 던져 요청을 만들지 않는다
    const sig = path + ' ' + JSON.stringify(body);
    const key = pending[sig]?.key || uid();
    pending[sig] = { path, body, key };
    save();
    track(sig, 1);
    let r;
    try {
      r = await fetch(path, { method: 'POST', body: JSON.stringify({ ...body, key, actor: who }) });
    } catch {
      track(sig, -1);
      onChange();  // 첫 실패 직후에도 재전송 안내가 바로 보이게 한다
      throw new Error('응답을 받지 못했습니다. 같은 내용으로 다시 보내면 중복 없이 처리됩니다.');
    }
    track(sig, -1);
    const j = await r.json().catch(() => ({}));
    // 결과가 확정된 요청만 지운다. 늦게 온 응답이 그 사이 같은 내용으로 시작된 다른 요청(다른 키)을 지우지 않게 키를 확인한다.
    if (r.status < 500 && pending[sig]?.key === key) { delete pending[sig]; save(); }
    onChange();
    if (!r.ok) throw new Error(j.error || '요청을 처리하지 못했습니다');
    return j;
  }

  return {
    send,
    async resend() { for (const { path, body } of Object.values(pending)) await send(path, body); },
    unresolved: () => Object.keys(pending).filter(sig => !inflight.has(sig)).length,
    persisted: () => persisted,
  };
}

if (typeof module !== 'undefined') module.exports = { intOf, createSender };
