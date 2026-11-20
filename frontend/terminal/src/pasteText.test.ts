import assert from 'node:assert/strict';
import test from 'node:test';

import {
	expandPastePlaceholders,
	formatPastePlaceholder,
	placeholderEndingAt,
	placeholderStartingAt,
	shouldCollapsePaste,
} from './pasteText.js';

test('collapses long single-line pastes but not normal typing', () => {
	assert.equal(shouldCollapsePaste('hello'), false);
	assert.equal(shouldCollapsePaste('x'.repeat(399)), false);
	assert.equal(shouldCollapsePaste('x'.repeat(400)), true);
});

test('collapses multi-line pastes', () => {
	assert.equal(shouldCollapsePaste('one\ntwo\nthree\nfour'), true);
	assert.equal(shouldCollapsePaste('one\ntwo'), false);
});

test('formats line and char placeholders', () => {
	assert.equal(formatPastePlaceholder(1, 'a\nb\nc'), '[Pasted text #1 +2 lines]');
	assert.equal(formatPastePlaceholder(2, 'x'.repeat(1200)), '[Pasted text #2 (1200 chars)]');
});

test('expands known placeholders and keeps unknown ones', () => {
	const body = 'line 1\nline 2\nline 3\nline 4';
	const token = formatPastePlaceholder(1, body);
	const blocks = new Map([[token, body]]);
	assert.equal(expandPastePlaceholders(`explain this:\n${token}`, blocks), `explain this:\n${body}`);
	assert.equal(
		expandPastePlaceholders('[Pasted text #9 +9 lines]', blocks),
		'[Pasted text #9 +9 lines]',
	);
	assert.equal(expandPastePlaceholders('plain text', blocks), 'plain text');
});

test('finds placeholders adjacent to the caret', () => {
	const value = 'see [Pasted text #3 +7 lines] end';
	const end = value.indexOf(' end');
	assert.deepEqual(placeholderEndingAt(value, end), {
		start: value.indexOf('[Pasted'),
		token: '[Pasted text #3 +7 lines]',
	});
	assert.equal(placeholderEndingAt(value, end - 1), null);
	assert.deepEqual(placeholderStartingAt(value, value.indexOf('[Pasted')), {
		end,
		token: '[Pasted text #3 +7 lines]',
	});
	assert.equal(placeholderStartingAt(value, 0), null);
});
