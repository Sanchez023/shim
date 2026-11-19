import assert from 'node:assert/strict';
import test from 'node:test';

import {buildTerminalTitle, terminalTitleSequence} from './terminalTitle.js';

test('shows connecting, idle, working, and waiting states', () => {
	assert.match(
		buildTerminalTitle({appName: 'oh', ready: false, busy: false}),
		/connecting/,
	);
	assert.equal(
		buildTerminalTitle({
			appName: 'oh',
			ready: true,
			busy: false,
			model: 'deepseek-v4-flash',
		}),
		'○ oh · idle · deepseek-v4-flash',
	);
	assert.equal(
		buildTerminalTitle({
			appName: 'oh',
			ready: true,
			busy: true,
			busyLabel: 'Running bash...',
			model: 'deepseek-v4-flash',
		}),
		'● oh · Running bash... · deepseek-v4-flash',
	);
	assert.match(
		buildTerminalTitle({appName: 'oh', ready: true, busy: true, waitingForInput: true}),
		/waiting for input/,
	);
});

test('includes the queued count and honors the app name', () => {
	const title = buildTerminalTitle({
		appName: 'ohmo',
		ready: true,
		busy: false,
		model: 'kimi-k2.5',
		queuedCount: 2,
	});

	assert.equal(title, '○ ohmo · idle · kimi-k2.5 · +2 queued');
});

test('sanitizes control characters before writing OSC 0', () => {
	const title = buildTerminalTitle({
		appName: 'oh\u0007]0;evil',
		ready: true,
		busy: true,
		busyLabel: 'line\u001B[31mred',
	});

	assert.doesNotMatch(title, /[\u0000-\u001F\u007F]/);
	const sequence = terminalTitleSequence(title);
	assert.match(sequence, /^\u001B\]0;.*\u0007$/);
	assert.doesNotMatch(sequence.slice(4, -1), /[\u0000-\u001F\u007F]/);
});
