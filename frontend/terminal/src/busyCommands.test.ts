import assert from 'node:assert/strict';
import test from 'node:test';

import {isBusySafeCommand} from './busyCommands.js';

const allowed = ['/status', '/cost', '/diff'];

test('recognizes allow-listed slash commands', () => {
	assert.equal(isBusySafeCommand('/status', allowed), true);
	assert.equal(isBusySafeCommand('  /cost  ', allowed), true);
	assert.equal(isBusySafeCommand('/diff HEAD~1', allowed), true);
});

test('ignores commands outside the allow list', () => {
	assert.equal(isBusySafeCommand('/clear', allowed), false);
	assert.equal(isBusySafeCommand('/resume abc', allowed), false);
});

test('ignores plain prompts, shell lines and empty input', () => {
	assert.equal(isBusySafeCommand('fix the bug', allowed), false);
	assert.equal(isBusySafeCommand('!ls', allowed), false);
	assert.equal(isBusySafeCommand('/', allowed), false);
	assert.equal(isBusySafeCommand('', allowed), false);
});

test('is case insensitive and tolerant of an empty allow list', () => {
	assert.equal(isBusySafeCommand('/STATUS', allowed), true);
	assert.equal(isBusySafeCommand('/status', []), false);
});
