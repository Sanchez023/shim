import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React, {useRef, useState} from 'react';
import {render} from 'ink';

import {
	expandPastePlaceholders,
	formatPastePlaceholder,
	shouldCollapsePaste,
} from '../pasteText.js';
import {ThemeProvider} from '../theme/ThemeContext.js';
import {PromptInput} from './PromptInput.js';

const nextLoopTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

type InkTestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};

type InkTestStdin = PassThrough & {
	isTTY: boolean;
	setRawMode: (_mode: boolean) => void;
	resume: () => InkTestStdin;
	pause: () => InkTestStdin;
	ref: () => InkTestStdin;
	unref: () => InkTestStdin;
};

function createTestStdout(): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns: 120,
		rows: 40,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

function createTestStdin(): InkTestStdin {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		setRawMode: () => undefined,
		resume() {
			return this;
		},
		pause() {
			return this;
		},
		ref() {
			return this;
		},
		unref() {
			return this;
		},
	});
}

async function sendKey(stdin: InkTestStdin, chunk: string | Buffer): Promise<void> {
	stdin.write(chunk);
	await nextLoopTurn();
	await nextLoopTurn();
}

async function waitForValue(getValue: () => string, expected: string): Promise<void> {
	for (let i = 0; i < 50; i += 1) {
		await nextLoopTurn();
		if (getValue() === expected) {
			return;
		}
	}

	assert.equal(getValue(), expected);
}

interface HarnessHandles {
	instance: ReturnType<typeof render>;
	stdin: InkTestStdin;
	stop: () => Promise<void>;
}

function startHarness(options: {
	onInputChange: (value: string) => void;
	onSubmit?: (value: string) => void;
}): HarnessHandles {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const blocks = new Map<string, string>();

	function Harness(): React.JSX.Element {
		const [input, setInput] = useState('');
		const blocksRef = useRef(blocks);
		const counterRef = useRef(0);

		return (
			<ThemeProvider initialTheme="default">
				<PromptInput
					busy={false}
					input={input}
					setInput={(value) => {
						options.onInputChange(value);
						setInput(value);
					}}
					onSubmit={(value) => {
						options.onSubmit?.(expandPastePlaceholders(value, blocksRef.current));
						setInput('');
					}}
					collapsePaste={(text) => {
						if (!shouldCollapsePaste(text)) {
							return null;
						}
						counterRef.current += 1;
						const token = formatPastePlaceholder(counterRef.current, text);
						blocksRef.current.set(token, text);
						return token;
					}}
				/>
			</ThemeProvider>
		);
	}

	const instance = render(<Harness />, {
		stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
		stdout: stdout as unknown as NodeJS.WriteStream,
		debug: true,
		patchConsole: false,
	});
	const exitPromise = instance.waitUntilExit();

	return {
		instance,
		stdin,
		stop: async () => {
			instance.unmount();
			await exitPromise;
			instance.cleanup();
			stdin.destroy();
			stdout.destroy();
		},
	};
}

function multiline(count: number, prefix: string): string {
	return Array.from({length: count}, (_, index) => `${prefix} ${index + 1}`).join('\n');
}

test('collapses a large paste into a placeholder and expands it on submit', async () => {
	let currentValue = '';
	let submitted = '';
	const harness = startHarness({
		onInputChange: (value) => {
			currentValue = value;
		},
		onSubmit: (value) => {
			submitted = value;
		},
	});

	try {
		await nextLoopTurn();

		const pasted = multiline(10, 'pasted line');
		await sendKey(harness.stdin, 'explain:');
		await waitForValue(() => currentValue, 'explain:');
		await sendKey(harness.stdin, pasted);
		await waitForValue(() => currentValue, 'explain:[Pasted text #1 +9 lines]');

		await sendKey(harness.stdin, '\r');
		await waitForValue(() => submitted, `explain:${pasted}`);
	} finally {
		await harness.stop();
	}
});

test('backspace removes a collapsed paste as one token', async () => {
	let currentValue = '';
	const harness = startHarness({
		onInputChange: (value) => {
			currentValue = value;
		},
	});

	try {
		await nextLoopTurn();

		await sendKey(harness.stdin, multiline(8, 'row'));
		await waitForValue(() => currentValue, '[Pasted text #1 +7 lines]');

		await sendKey(harness.stdin, Buffer.from([0x7f]));
		await waitForValue(() => currentValue, '');
	} finally {
		await harness.stop();
	}
});

test('keeps ordinary typing literal', async () => {
	let currentValue = '';
	const harness = startHarness({
		onInputChange: (value) => {
			currentValue = value;
		},
	});

	try {
		await nextLoopTurn();

		await sendKey(harness.stdin, 'hello');
		await waitForValue(() => currentValue, 'hello');
	} finally {
		await harness.stop();
	}
});
