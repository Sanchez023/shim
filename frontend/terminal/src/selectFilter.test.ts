import assert from 'node:assert/strict';
import test from 'node:test';

import {filterSelectOptions, scoreSelectOption} from './selectFilter.js';

const options = [
	{value: 'a', label: '11/18 09:12  42msg  refactor auth module'},
	{value: 'b', label: '11/19 10:04  8msg  fix resume context loss'},
	{value: 'c', label: '11/17 22:31  3msg  (no summary)', description: 'deepseek-chat'},
];

test('empty query keeps every option in order', () => {
	assert.deepEqual(filterSelectOptions(options, '').map((o) => o.value), ['a', 'b', 'c']);
	assert.deepEqual(filterSelectOptions(options, '   ').map((o) => o.value), ['a', 'b', 'c']);
});

test('substring matches on the label rank before description matches', () => {
	assert.deepEqual(filterSelectOptions(options, 'resume').map((o) => o.value), ['b']);
	assert.deepEqual(filterSelectOptions(options, 'deepseek').map((o) => o.value), ['c']);
});

test('matching is case insensitive', () => {
	assert.deepEqual(filterSelectOptions(options, 'AUTH').map((o) => o.value), ['a']);
});

test('fuzzy subsequence matches still find sessions', () => {
	// "rfc" is not a substring of "refactor" but is a subsequence of it.
	assert.deepEqual(filterSelectOptions(options, 'rfctr').map((o) => o.value), ['a']);
});

test('unmatched queries return nothing', () => {
	assert.deepEqual(filterSelectOptions(options, 'zzzz'), []);
});

test('earlier substring hits outrank later ones', () => {
	const early = scoreSelectOption({value: 'x', label: 'fix resume bug'}, 'resume');
	const late = scoreSelectOption({value: 'y', label: 'a very long title that mentions resume'}, 'resume');
	assert.ok(early !== null && late !== null);
	assert.ok(early > late);
});

test('label hits outrank description-only hits', () => {
	const label = scoreSelectOption({value: 'x', label: 'auth refactor'}, 'auth');
	const description = scoreSelectOption({value: 'y', label: 'unrelated', description: 'auth refactor'}, 'auth');
	assert.ok(label !== null && description !== null);
	assert.ok(label > description);
});
