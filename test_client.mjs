// client.js 테스트(2차 #8·#9, 3차 #2·#3·#7). 실행: node --test test_client.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { intOf, createSender } = createRequire(import.meta.url)('./client.js');

function memoryStorage() {
  const m = new Map();
  return { getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, v) };
}
const brokenStorage = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };

// 요청마다 응답을 직접 풀어 줄 수 있는 가짜 fetch. 보낸 키를 기록한다.
function controlledFetch() {
  const calls = [];
  const fetch = (path, opts) => new Promise((resolve, reject) => {
    calls.push({
      key: JSON.parse(opts.body).key,
      ok: (status = 200) => resolve({ ok: status < 400, status, json: async () => ({}) }),
      lost: () => reject(new TypeError('network')),
    });
  });
  return { fetch, calls };
}
const ids = () => { let n = 0; return () => `k${++n}`; };
const tick = () => new Promise(r => setImmediate(r));

test('intOf는 전체가 정수일 때만 받는다', () => {
  assert.equal(intOf('7', 1, '수량'), 7);
  assert.equal(intOf(' 0 ', 0, '실사'), 0);
  for (const bad of ['1.9', '1e3', '2abc', '', '-1', '1000001', null]) {
    assert.throws(() => intOf(bad, 1, '수량'), /정수/, String(bad));
  }
});

test('처리 중 더블클릭은 같은 키, 끝난 뒤 같은 내용은 새 키', async () => {
  const { fetch, calls } = controlledFetch();
  const s = createSender({ fetch, storage: memoryStorage(), uid: ids() });
  const a = s.send('/api/pos', { item_id: 1, qty: 7 });
  const b = s.send('/api/pos', { item_id: 1, qty: 7 });
  await tick();
  calls[0].ok(); calls[1].ok();
  await Promise.all([a, b]);
  s.send('/api/pos', { item_id: 1, qty: 7 }); await tick();
  assert.deepEqual(calls.map(c => c.key), ['k1', 'k1', 'k2']);
});

test('3차 #2: 늦게 온 응답이 다음 요청의 키를 지우지 않는다', async () => {
  const { fetch, calls } = controlledFetch();
  const s = createSender({ fetch, storage: memoryStorage(), uid: ids() });
  const body = { item_id: 1, qty: 7 };
  const a1 = s.send('/api/pos', body), a2 = s.send('/api/pos', body);
  await tick();
  calls[0].ok(); await a1;                 // A1 성공 → 지워짐
  const b = s.send('/api/pos', body);      // 같은 내용의 다음 작업 B → k2
  await tick();
  calls[1].ok(); await a2;                 // 늦은 A2 성공 응답
  calls[2].lost(); await b.catch(() => {}); // B는 응답 유실
  s.send('/api/pos', body); await tick();  // B 재전송은 k2여야 한다
  assert.deepEqual(calls.map(c => c.key), ['k1', 'k1', 'k2', 'k2']);
});

test('응답 유실 뒤 새로고침해도 같은 키로 다시 보낸다', async () => {
  const storage = memoryStorage();
  const first = controlledFetch();
  const s1 = createSender({ fetch: first.fetch, storage, uid: ids() });
  const p = s1.send('/api/pos', { item_id: 1, qty: 7 }); await tick();
  first.calls[0].lost();
  await assert.rejects(p, /응답을 받지 못했습니다/);
  const again = controlledFetch();          // 새로고침: 새 sender가 storage에서 복원
  const s2 = createSender({ fetch: again.fetch, storage, uid: () => 'new' });
  assert.equal(s2.unresolved(), 1);
  const r = s2.resend(); await tick(); again.calls[0].ok(); await r;
  assert.equal(again.calls[0].key, 'k1');
  assert.equal(s2.unresolved(), 0);
});

test('3차 #3: 저장소를 못 써도 메모리로 같은 키를 쓰고, 보존 실패를 알린다', async () => {
  const { fetch, calls } = controlledFetch();
  const s = createSender({ fetch, storage: brokenStorage, uid: ids() });
  const a = s.send('/api/pos', { item_id: 1, qty: 7 });
  const b = s.send('/api/pos', { item_id: 1, qty: 7 });
  await tick();
  calls[0].lost(); calls[1].lost();
  await Promise.allSettled([a, b]);
  s.send('/api/pos', { item_id: 1, qty: 7 }); await tick();
  assert.deepEqual(calls.map(c => c.key), ['k1', 'k1', 'k1']);
  assert.equal(s.persisted(), false);
});

test('3차 #7: 첫 실패 직후 재전송 안내가 바로 갱신된다', async () => {
  const { fetch, calls } = controlledFetch();
  const seen = [];
  const s = createSender({ fetch, storage: memoryStorage(), uid: ids(), onChange: () => seen.push(s.unresolved()) });
  const p = s.send('/api/pos', { item_id: 1, qty: 7 }); await tick();
  assert.equal(s.unresolved(), 0);          // 응답 대기 중에는 안내하지 않는다
  calls[0].lost(); await p.catch(() => {});
  assert.deepEqual(seen, [1]);
});

test('400 응답은 결과가 확정된 것이라 지우고, 500은 남긴다', async () => {
  const { fetch, calls } = controlledFetch();
  const s = createSender({ fetch, storage: memoryStorage(), uid: ids() });
  let p = s.send('/api/pos', { item_id: 1, qty: 7 }); await tick();
  calls[0].ok(400); await p.catch(() => {});
  assert.equal(s.unresolved(), 0);
  p = s.send('/api/pos', { item_id: 1, qty: 8 }); await tick();
  calls[1].ok(500); await p.catch(() => {});
  assert.equal(s.unresolved(), 1);
});

test('작업자 이름을 요청에 싣고, 없으면 요청을 만들지 않는다', async () => {
  const bodies = [];
  const fetch = async (path, opts) => { bodies.push(JSON.parse(opts.body)); return { ok: true, status: 200, json: async () => ({}) }; };
  let who = '';
  const s = createSender({ fetch, storage: memoryStorage(), uid: ids(), actor: () => { if (!who) throw new Error('작업자'); return who; } });
  await assert.rejects(s.send('/api/pos', { item_id: 1, qty: 7 }), /작업자/);
  assert.equal(bodies.length, 0);
  assert.equal(s.unresolved(), 0);  // 보내지 않은 요청이 재전송 대기에 남지 않는다
  who = '민수';
  await s.send('/api/pos', { item_id: 1, qty: 7 });
  assert.deepEqual(bodies, [{ item_id: 1, qty: 7, key: 'k1', actor: '민수' }]);
});
