import assert from 'node:assert/strict';
import {PassThrough} from 'node:stream';
import test from 'node:test';
import ansiEscapes from 'ansi-escapes';

import instances from '../node_modules/ink/build/instances.js';
import {
	installResizeRepair,
	physicalRows,
	reflowEraseSequence,
} from './resizeRepaint.js';

const long = 'word '.repeat(30).trim();

type TestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
};

function createStdout(columns: number): TestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns,
		rows: 40,
	});
}

test('physicalRows wraps each logical line at the column boundary', () => {
	assert.equal(physicalRows('', 80), 0);
	assert.equal(physicalRows('short', 80), 1);
	assert.equal(physicalRows('x'.repeat(80), 80), 1);
	assert.equal(physicalRows('x'.repeat(81), 80), 2);
	// A blank line still occupies a terminal row.
	assert.equal(physicalRows('a\n\nb', 80), 3);
});

test('reflowEraseSequence erases the rows narrowing added', () => {
	// The frame is three logical rows at 120 columns; at 60 the first row wraps
	// into two more, so Ink's count is short by two rows.
	const frame = `${long}\n${'y'.repeat(20)}\nstatus line`;
	const rowsAt120 = physicalRows(frame, 120);
	const rowsAt60 = physicalRows(frame, 60);
	assert.ok(rowsAt60 > rowsAt120);

	assert.equal(
		reflowEraseSequence(frame, 120, 60),
		ansiEscapes.eraseLines(rowsAt60 - rowsAt120 + 1),
	);
});

test('reflowEraseSequence does nothing when the frame does not grow', () => {
	const frame = 'short line\nstatus line';
	assert.equal(reflowEraseSequence(frame, 40, 120), '');
	assert.equal(reflowEraseSequence(frame, 80, 80), '');
	assert.equal(reflowEraseSequence('', 80, 40), '');
});

test('installResizeRepair runs before Ink and erases the overflow rows', () => {
	const stdout = createStdout(120);
	const writes: string[] = [];
	stdout.on('data', (chunk) => writes.push(String(chunk)));

	const order: string[] = [];
	const ink = {
		lastOutput: `${long}\n${'y'.repeat(20)}\nstatus line`,
		resized: () => {
			order.push('ink');
		},
	};
	// Stand in for the instance Ink registers for this stream.
	instances.set(stdout as unknown as NodeJS.WriteStream, ink as never);
	// Ink's own listener was registered first; the repair must take it over.
	stdout.on('resize', ink.resized);

	const cleanup = installResizeRepair(stdout as unknown as NodeJS.WriteStream);
	const listenerNames = stdout.listeners('resize').map((listener) => listener.name);
	assert.equal(listenerNames[0], 'handleResize');

	stdout.columns = 60;
	stdout.emit('resize');

	assert.deepEqual(order, ['ink']);
	const delta =
		physicalRows(ink.lastOutput, 60) - physicalRows(ink.lastOutput, 120);
	assert.equal(writes.join(''), ansiEscapes.eraseLines(delta + 1));

	// Widening back must not emit anything.
	writes.length = 0;
	stdout.columns = 120;
	stdout.emit('resize');
	assert.deepEqual(order, ['ink', 'ink']);
	assert.equal(writes.join(''), '');

	cleanup();
	instances.delete(stdout as unknown as NodeJS.WriteStream);
	assert.equal(
		stdout.listeners('resize').filter((listener) => listener.name === 'handleResize')
			.length,
		0,
	);
});

test('installResizeRepair is a no-op for non-TTY streams', () => {
	const stdout = Object.assign(new PassThrough(), {isTTY: false, columns: 80, rows: 24});
	const writes: string[] = [];
	stdout.on('data', (chunk) => writes.push(String(chunk)));

	const cleanup = installResizeRepair(stdout as unknown as NodeJS.WriteStream);
	stdout.columns = 40;
	stdout.emit('resize');

	assert.equal(writes.join(''), '');
	cleanup();
});
