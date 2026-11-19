import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {ThemeProvider} from '../theme/ThemeContext.js';
import {WelcomeBanner} from './WelcomeBanner.js';

const stripAnsi = (value: string): string => value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');
const nextLoopTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

type InkTestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};

function createTestStdout(columns: number): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns,
		rows: 40,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

async function renderBanner(columns: number, appName = 'shim'): Promise<string> {
	const stdout = createTestStdout(columns);
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(
		<ThemeProvider initialTheme="default">
			<WelcomeBanner appName={appName} version="0.2.0" />
		</ThemeProvider>,
		{stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false},
	);
	const exitPromise = instance.waitUntilExit();

	let previous = '';
	for (let index = 0; index < 50; index++) {
		await nextLoopTurn();
		if (output.length > 0 && output === previous) {
			break;
		}
		previous = output;
	}

	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdout.destroy();
	return stripAnsi(previous);
}

test('shows the wordmark and version on wide terminals', async () => {
	const output = await renderBanner(120);

	assert.match(output, /v0\.2\.0/);
	assert.match(output, /\/help/);
	assert.match(output, /An AI-powered coding assistant/);
	assert.match(output, /██/);
});

test('falls back to a compact header on narrow terminals', async () => {
	const output = await renderBanner(30);

	assert.match(output, /◆ SHIM/);
	assert.match(output, /v0\.2\.0/);
	assert.doesNotMatch(output, /██/);
});

test('uses the app name for ohmo', async () => {
	const output = await renderBanner(120, 'ohmo');

	assert.match(output, /OHMO|██/);
	assert.match(output, /v0\.2\.0/);
});
