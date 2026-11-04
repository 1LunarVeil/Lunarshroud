import test from 'node:test';
import assert from 'node:assert/strict';

import { buildChatStats } from '../js/chat_stream.mjs';

/**
 * "思考吃光预算"的判定逻辑。
 *
 * 这段逻辑目前在 chat.js 里内联（它依赖流式状态），但判定条件本身是纯的，
 * 所以这里用等价的纯函数把**契约**钉住：什么组合算"思考耗尽"，什么不算。
 *
 * 背景（2026-10-30 实测，Qwen3.5-4B）：
 *   请求不带 chat_template_kwargs.enable_thinking 时，模板默认开启思考；
 *   思考与正文共享 max_tokens，预算被思考吃光后 finish_reason=length 且
 *   content 为空。界面上看起来就是"卡住 / 死循环 / 变笨"，而 WebUI 用另一套
 *   请求体所以没有这个问题。
 */
function thinkingExhausted({ content, reasoning, finishReason }) {
  return (
    !String(content || '').trim() &&
    String(reasoning || '').trim().length > 0 &&
    String(finishReason || '') === 'length'
  );
}

test('thinking that consumed the budget is flagged', () => {
  assert.equal(
    thinkingExhausted({
      content: '',
      reasoning: '1. Analyze... 2. Compare... 3. Final answer: 9.9 is bigger',
      finishReason: 'length',
    }),
    true
  );
  // 只有空白正文也算"没有正文"
  assert.equal(
    thinkingExhausted({ content: '   \n\t', reasoning: 'thinking...', finishReason: 'length' }),
    true
  );
});

test('a normal answer with thinking is NOT flagged', () => {
  assert.equal(
    thinkingExhausted({
      content: '9.9 更大，因为十分位 9 > 1。',
      reasoning: 'some thinking',
      finishReason: 'stop',
    }),
    false
  );
  // 想完了并且答案也出来了，即使被截断也不算"耗尽"—— 用户拿到了正文
  assert.equal(
    thinkingExhausted({
      content: '部分答案……',
      reasoning: 'some thinking',
      finishReason: 'length',
    }),
    false
  );
});

test('empty answer without any thinking is a different failure', () => {
  // 没有思考也没有正文，通常是真正的生成失败，不该归因到"思考耗尽"
  assert.equal(
    thinkingExhausted({ content: '', reasoning: '', finishReason: 'length' }),
    false
  );
  assert.equal(
    thinkingExhausted({ content: '', reasoning: '', finishReason: 'stop' }),
    false
  );
});

test('finishReason stop with only reasoning is not the length path', () => {
  // 模型自己决定只输出思考就停（少见但可能），那不是预算问题
  assert.equal(
    thinkingExhausted({ content: '', reasoning: 'only thoughts', finishReason: 'stop' }),
    false
  );
  assert.equal(
    thinkingExhausted({ content: '', reasoning: 'only thoughts', finishReason: '' }),
    false
  );
});

test('buildChatStats still records the numbers the diagnosis depends on', () => {
  // 判定的输入来自流式统计；这条防止将来重命名字段时判定静默失效
  const stats = buildChatStats({
    usage: { completion_tokens: 1630, prompt_tokens: 40 },
    timings: null,
    elapsedMs: 120000,
    content: '',
    reasoningChars: 7469,
  });
  assert.equal(stats.completion_tokens, 1630);
  assert.equal(stats.reasoning_chars, 7469);
});
