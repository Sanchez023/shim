import {common, createLowlight} from 'lowlight';

import type {ThemeConfig} from './theme/builtinThemes.js';

export type StyledSpan = {
	text: string;
	color?: string;
	bold?: boolean;
	italic?: boolean;
};

type SpanStyle = {
	color?: string;
	bold?: boolean;
	italic?: boolean;
};

const lowlight = createLowlight(common);

const KEYWORDS = new Set([
	'hljs-keyword',
	'hljs-selector-tag',
	'hljs-literal',
	'hljs-built_in',
	'hljs-name',
	'hljs-tag',
]);
const STRINGS = new Set([
	'hljs-string',
	'hljs-attr',
	'hljs-attribute',
	'hljs-template-tag',
	'hljs-template-variable',
	'hljs-regexp',
]);
const NUMBERS = new Set(['hljs-number', 'hljs-symbol', 'hljs-bullet', 'hljs-link']);
const COMMENTS = new Set(['hljs-comment', 'hljs-quote']);
const FUNCTIONS = new Set([
	'hljs-title',
	'hljs-section',
	'hljs-function',
	'hljs-selector-id',
	'hljs-selector-class',
]);
const TYPES = new Set(['hljs-type', 'hljs-class', 'hljs-variable', 'hljs-params', 'hljs-property']);

function styleFor(className: string, theme: ThemeConfig): SpanStyle {
	for (const name of className.split(/\s+/)) {
		if (KEYWORDS.has(name)) {
			return {color: theme.colors.primary, bold: true};
		}
		if (STRINGS.has(name)) {
			return {color: theme.colors.success};
		}
		if (NUMBERS.has(name)) {
			return {color: theme.colors.warning};
		}
		if (COMMENTS.has(name)) {
			return {color: theme.colors.muted, italic: true};
		}
		if (FUNCTIONS.has(name)) {
			return {color: theme.colors.info};
		}
		if (TYPES.has(name)) {
			return {color: theme.colors.secondary, bold: true};
		}
		if (name === 'hljs-meta') {
			return {color: theme.colors.muted};
		}
		if (name === 'hljs-addition') {
			return {color: theme.colors.success};
		}
		if (name === 'hljs-deletion') {
			return {color: theme.colors.error};
		}
	}
	return {};
}

type HastNode = {
	type?: string;
	value?: string;
	tagName?: string;
	properties?: {className?: unknown};
	children?: HastNode[];
};

function walk(node: HastNode, inherited: SpanStyle, theme: ThemeConfig, out: StyledSpan[]): void {
	if (node.type === 'text') {
		if (node.value) {
			out.push({text: node.value, ...inherited});
		}
		return;
	}
	const className = Array.isArray(node.properties?.className)
		? node.properties.className.filter((value): value is string => typeof value === 'string').join(' ')
		: '';
	const style = className ? {...inherited, ...styleFor(className, theme)} : inherited;
	for (const child of node.children ?? []) {
		walk(child, style, theme, out);
	}
}

/**
 * Highlight a fenced code block into one array of styled spans per line.
 *
 * Unknown or missing languages fall back to unhighlighted text so the block
 * still renders; the caller adds the border and line cap.
 */
export function highlightToLines(
	code: string,
	language: string | undefined,
	theme: ThemeConfig,
): StyledSpan[][] {
	const normalized = (language ?? '').trim().toLowerCase().split(/\s+/)[0] ?? '';
	const source = code.replace(/\t/g, '  ');
	const spans: StyledSpan[] = [];

	if (normalized && lowlight.registered(normalized)) {
		const tree = lowlight.highlight(normalized, source) as HastNode;
		walk(tree, {color: theme.colors.foreground}, theme, spans);
	} else if (source) {
		spans.push({text: source, color: theme.colors.foreground});
	}

	const lines: StyledSpan[][] = [[]];
	for (const span of spans) {
		const parts = span.text.split('\n');
		parts.forEach((part, index) => {
			if (index > 0) {
				lines.push([]);
			}
			if (part.length > 0) {
				lines[lines.length - 1]!.push({...span, text: part});
			}
		});
	}
	return lines;
}
