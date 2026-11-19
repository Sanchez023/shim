import assert from 'node:assert/strict';
import test from 'node:test';

import {installSynchronizedOutput} from './synchronizedOutput.js';

function fakeStream(isTTY: boolean): NodeJS.WriteStream & {writes: string[]} {
	const writes: string[] = [];
	const stream = {
		isTTY,
		writes,
		write(chunk: unknown) {
			writes.push(String(chunk));
			return true;
		},
	} as unknown as NodeJS.WriteStream & {writes: string[]};
	return stream;
}

test('wraps Ink writes in synchronized-output markers', () => {
	const stream = fakeStream(true);
	const restore = installSynchronizedOutput(stream);
	try {
		stream.write('\u001B[2Kframe');
		assert.deepEqual(stream.writes, ['\u001B[?2026h\u001B[2Kframe\u001B[?2026l']);
	} finally {
		restore();
	}
});

test('leaves non-TTY streams untouched', () => {
	const stream = fakeStream(false);
	const restore = installSynchronizedOutput(stream);
	try {
		stream.write('plain');
		assert.deepEqual(stream.writes, ['plain']);
	} finally {
		restore();
	}
});
