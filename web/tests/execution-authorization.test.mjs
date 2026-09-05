import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../lib/execution-authorization.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } });
const { authorizeExecution } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

test('live cancel cannot call the authorization endpoint', () => {
  const events = [];
  assert.equal(authorizeExecution(true, 'REAL ORDER', () => events.push('send'), message => {
    events.push(message);
    return false;
  }), false);
  assert.deepEqual(events, ['REAL ORDER']);
});

test('live confirmation occurs before the consequential action, exactly once', () => {
  const events = [];
  assert.equal(authorizeExecution(true, 'REAL ORDER', () => events.push('send'), message => {
    events.push(message);
    return true;
  }), true);
  assert.deepEqual(events, ['REAL ORDER', 'send']);
});

test('shadow approval preserves the non-live workflow', () => {
  const events = [];
  assert.equal(authorizeExecution(false, '', () => events.push('send'), () => {
    throw new Error('shadow must not request live confirmation');
  }), true);
  assert.deepEqual(events, ['send']);
});
