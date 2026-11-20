import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';
import stringWidth from 'string-width';

import {SelectModal} from './SelectModal.js';

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

function createTestStdout(columns = 120): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns,
		rows: 40,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

async function waitForOutputToStabilize(getOutput: () => string): Promise<string> {
	let previous = '';
	let sawOutput = false;

	for (let i = 0; i < 50; i += 1) {
		await nextLoopTurn();
		const current = getOutput();
		sawOutput ||= current.length > 0;
		if (sawOutput && current === previous) {
			return current;
		}
		previous = current;
	}

	throw new Error(`Ink output did not stabilize: ${JSON.stringify(previous)}`);
}

async function renderToText(element: React.JSX.Element, columns = 120): Promise<string> {
	const stdout = createTestStdout(columns);
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(element, {
		stdout: stdout as unknown as NodeJS.WriteStream,
		debug: true,
		patchConsole: false,
	});
	const exitPromise = instance.waitUntilExit();
	const stable = await waitForOutputToStabilize(() => output);
	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdout.destroy();

	return stripAnsi(stable);
}

test('session picker renders a search line and the match counter', async () => {
	const rendered = await renderToText(
		<SelectModal
			title="Resume Session (2)"
			options={[{value: 'abc', label: '11/18 09:12  42msg  refactor auth', description: 'deepseek-chat'}]}
			selectedIndex={0}
			searchable
			query="auth"
			totalCount={12}
		/>,
	);

	assert.match(rendered, /Resume Session \(2\)/);
	assert.match(rendered, /search auth/);
	assert.match(rendered, /1\/12 matches/);
	assert.match(rendered, /type to search/);
	assert.match(rendered, /11\/18 09:12 {2}42msg {2}refactor auth/);
});

test('non-searchable pickers keep the plain footer and show no search line', async () => {
	const rendered = await renderToText(
		<SelectModal
			title="Permission Mode"
			options={[{value: 'plan', label: 'Plan', active: true}]}
			selectedIndex={0}
		/>,
	);

	assert.doesNotMatch(rendered, /search/);
	assert.match(rendered, /\(current\)/);
	assert.match(rendered, /navigate/);
});

test('an empty match set renders the no-matches state', async () => {
	const rendered = await renderToText(
		<SelectModal title="Resume Session" options={[]} selectedIndex={0} searchable query="zzz" totalCount={7} />,
	);

	assert.match(rendered, /no matches/);
	assert.match(rendered, /0\/7 matches/);
});

test('clamps long labels so a picker row never wraps', async () => {
	const long = '11/20 14:30  75msg  把你的错别字改成正确的写法，并且顺手把那段说明也润色一下';
	const rendered = await renderToText(
		<SelectModal
			title="Resume Session (1)"
			options={[
				{
					value: 'abc',
					label: long,
					description: 'deepreasoning-ds-v4.1flash',
					active: true,
				},
			]}
			selectedIndex={0}
			searchable
			query=""
			totalCount={1}
		/>,
		60,
	);

	assert.match(rendered, /…/);
	// The clamp keeps the whole row (label + "(current)" + model) on one line.
	assert.match(rendered, /….*\(current\).*deepreasoning/);
	for (const line of rendered.split('\n')) {
		assert.ok(stringWidth(line) <= 60, `line overflows 60 columns: ${line.length}`);
	}
});
