import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildAcceptedHostValues,
  normalizeHostValue,
} from './bridge_helpers.js';

const TAILNET_HOST = 'yoyodine.wildebeest-algol.ts.net';

test('default (no env) rejects a non-loopback host', () => {
  const accepted = buildAcceptedHostValues(undefined);
  assert.equal(accepted.has(normalizeHostValue(TAILNET_HOST)), false);
});

test('default still accepts loopback hosts', () => {
  const accepted = buildAcceptedHostValues(undefined);
  assert.equal(accepted.has(normalizeHostValue('localhost')), true);
  assert.equal(accepted.has(normalizeHostValue('127.0.0.1')), true);
});

test('extra-host env accepts the tailnet host and keeps localhost', () => {
  const accepted = buildAcceptedHostValues(TAILNET_HOST);
  assert.equal(accepted.has(TAILNET_HOST), true);
  assert.equal(accepted.has('localhost'), true);
});

test('multiple comma-separated extra hosts are all accepted', () => {
  const accepted = buildAcceptedHostValues(
    `${TAILNET_HOST},rbmbp.wildebeest-algol.ts.net`,
  );
  assert.equal(accepted.has(TAILNET_HOST), true);
  assert.equal(accepted.has('rbmbp.wildebeest-algol.ts.net'), true);
});

test("a configured '*' is hard-rejected and never opens an all-hosts bypass", () => {
  const accepted = buildAcceptedHostValues('*');
  assert.equal(accepted.has('*'), false);
  assert.equal(accepted.has(normalizeHostValue(TAILNET_HOST)), false);
});

test('glob wildcard entries (e.g. *.ts.net) are rejected', () => {
  const accepted = buildAcceptedHostValues('*.ts.net');
  assert.equal(accepted.has('*.ts.net'), false);
  assert.equal(accepted.has(normalizeHostValue(TAILNET_HOST)), false);
});

test('whitespace and :port suffix are normalized away', () => {
  const accepted = buildAcceptedHostValues(
    ` ${TAILNET_HOST}:3000 , RBMBP.WILDEBEEST-ALGOL.TS.NET `,
  );
  assert.equal(accepted.has(TAILNET_HOST), true);
  assert.equal(accepted.has('rbmbp.wildebeest-algol.ts.net'), true);
});

test('empty entries are dropped', () => {
  const accepted = buildAcceptedHostValues(`,,  ${TAILNET_HOST}  ,,`);
  assert.equal(accepted.has(TAILNET_HOST), true);
});

test('normalizeHostValue strips port, brackets, and lowercases', () => {
  assert.equal(normalizeHostValue('  LocalHost:3000  '), 'localhost');
  assert.equal(normalizeHostValue('[::1]:3000'), '::1');
  assert.equal(
    normalizeHostValue('YOYODINE.WILDEBEEST-ALGOL.TS.NET'),
    'yoyodine.wildebeest-algol.ts.net',
  );
});
