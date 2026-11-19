import assert from 'node:assert/strict';
import test from 'node:test';

import {nextTodoPanel, parseTodoPanel} from './todoPanel.js';

test('known modes round-trip and unknown values fall back to full', () => {
	assert.equal(parseTodoPanel('full'), 'full');
	assert.equal(parseTodoPanel('compact'), 'compact');
	assert.equal(parseTodoPanel('hidden'), 'hidden');
	assert.equal(parseTodoPanel(undefined), 'full');
	assert.equal(parseTodoPanel(''), 'full');
	assert.equal(parseTodoPanel('nonsense'), 'full');
});

test('ctrl+t cycle is full -> compact -> hidden -> full', () => {
	assert.equal(nextTodoPanel('full'), 'compact');
	assert.equal(nextTodoPanel('compact'), 'hidden');
	assert.equal(nextTodoPanel('hidden'), 'full');
});
