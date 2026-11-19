import assert from 'node:assert/strict';
import test from 'node:test';

import {getBackspaceDeleteCount, nextGraphemeOffset, previousGraphemeOffset, splitGraphemes} from './PromptInput.js';

test('counts repeated backspace control characters from a single input chunk', () => {
	assert.equal(getBackspaceDeleteCount('\b\b\b'), 3);
	assert.equal(getBackspaceDeleteCount('\u007f\u007f'), 2);
	assert.equal(getBackspaceDeleteCount('\x7f\x7f\x7f'), 3);
});

test('falls back to a single delete for empty or unexpected input', () => {
	assert.equal(getBackspaceDeleteCount(''), 1);
	assert.equal(getBackspaceDeleteCount('abc'), 1);
	assert.equal(getBackspaceDeleteCount('\bA'), 1);
});

test('splits surrogate pairs and combining marks into graphemes', () => {
	const emoji = splitGraphemes('a😀b');
	assert.deepEqual(
		emoji.map((grapheme) => grapheme.text),
		['a', '😀', 'b'],
	);
	assert.deepEqual(
		emoji.map((grapheme) => [grapheme.start, grapheme.end]),
		[
			[0, 1],
			[1, 3],
			[3, 4],
		],
	);

	const combined = splitGraphemes('e\u0301x');
	assert.deepEqual(
		combined.map((grapheme) => grapheme.text),
		['e\u0301', 'x'],
	);
});

test('moves the cursor across whole graphemes', () => {
	const value = 'a😀b';

	assert.equal(previousGraphemeOffset(value, 4), 3);
	assert.equal(previousGraphemeOffset(value, 3), 1);
	assert.equal(previousGraphemeOffset(value, 1), 0);
	assert.equal(previousGraphemeOffset(value, 0), 0);

	assert.equal(nextGraphemeOffset(value, 0), 1);
	assert.equal(nextGraphemeOffset(value, 1), 3);
	assert.equal(nextGraphemeOffset(value, 3), 4);
	assert.equal(nextGraphemeOffset(value, 4), 4);

	assert.equal(nextGraphemeOffset('你好', 0), 1);
	assert.equal(previousGraphemeOffset('你好', 2), 1);
});
