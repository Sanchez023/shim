import assert from 'node:assert/strict';
import test from 'node:test';

import {nextTranscriptOffset} from './transcriptScroll.js';

test('returns null at the bottom so repeated paging does not re-render', () => {
	assert.equal(nextTranscriptOffset(0, 20, -1), null);
	assert.equal(nextTranscriptOffset(0, 20, -10), null);
	assert.equal(nextTranscriptOffset(0, 0, -1), null);
});

test('returns null at the top', () => {
	assert.equal(nextTranscriptOffset(20, 20, 1), null);
	assert.equal(nextTranscriptOffset(20, 20, 10), null);
});

test('clamps moves that cross an edge', () => {
	assert.equal(nextTranscriptOffset(5, 20, 1), 6);
	assert.equal(nextTranscriptOffset(5, 20, -10), 0);
	assert.equal(nextTranscriptOffset(15, 20, 10), 20);
	assert.equal(nextTranscriptOffset(5, 20, 100), 20);
});
