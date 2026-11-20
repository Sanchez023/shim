import {common, createLowlight} from 'lowlight';

import type {ThemeConfig} from './theme/builtinThemes.js';

export type StyledSpan = {
	text: string;
	color?: string;
	bold?: boolean;
	italic?: boolean;
	dim?: boolean;
	underline?: boolean;
};

type SpanStyle = {
	color?: string;
	bold?: boolean;
	italic?: boolean;
	dim?: boolean;
	underline?: boolean;
};

/**
 * Claude Code's dark-theme diff colors.
 *
 * In Claude's diff the add/remove colors are *backgrounds*; the bright "word"
 * variants highlight changed text.  The palette is fixed so the diff looks the
 * same under every app theme.
 */
export const CLAUDE_DIFF = {
	addedBackground: '#225c2b',
	removedBackground: '#7a2936',
	addedWord: '#38a660',
	removedWord: '#b3596b',
	text: '#ffffff',
	gutter: '#999999',
};

const lowlight = createLowlight(common);

const LANGUAGE_BY_EXTENSION: Record<string, string> = {
	ts: 'typescript',
	tsx: 'typescript',
	mts: 'typescript',
	cts: 'typescript',
	js: 'javascript',
	jsx: 'javascript',
	mjs: 'javascript',
	cjs: 'javascript',
	py: 'python',
	pyi: 'python',
	rb: 'ruby',
	go: 'go',
	rs: 'rust',
	java: 'java',
	kt: 'kotlin',
	kts: 'kotlin',
	c: 'c',
	h: 'c',
	cc: 'cpp',
	cpp: 'cpp',
	cxx: 'cpp',
	hpp: 'cpp',
	cs: 'csharp',
	php: 'php',
	swift: 'swift',
	sh: 'bash',
	bash: 'bash',
	zsh: 'bash',
	json: 'json',
	jsonc: 'json',
	yml: 'yaml',
	yaml: 'yaml',
	toml: 'ini',
	ini: 'ini',
	cfg: 'ini',
	md: 'markdown',
	markdown: 'markdown',
	css: 'css',
	scss: 'scss',
	less: 'less',
	html: 'xml',
	htm: 'xml',
	xml: 'xml',
	svg: 'xml',
	sql: 'sql',
	rsx: 'rust',
};

/** Best-effort language name for a file path, used by inline diff code. */
export function languageForPath(path: string | undefined): string | undefined {
	if (!path) {
		return undefined;
	}
	const cleaned = path.split(/[?#]/, 1)[0] ?? path;
	const name = cleaned.replace(/\\/g, '/').split('/').pop() ?? cleaned;
	const dot = name.lastIndexOf('.');
	if (dot < 0 || dot === name.length - 1) {
		return undefined;
	}
	return LANGUAGE_BY_EXTENSION[name.slice(dot + 1).toLowerCase()];
}

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

/**
 * highlight.js class -> Claude Code token style.
 *
 * Claude's diff highlighter maps keywords/literals/classes/names to bright
 * blue, built-ins/types/attributes to cyan, numbers/comments/additions to
 * green, strings/regexps/deletions to red, functions to yellow and meta/tags
 * to grey.  Other tokens inherit the normal white text color.
 */
function claudeStyleFor(className: string): SpanStyle {
	const names = new Set(className.split(/\s+/));
	if (names.has('hljs-title')) {
		if (names.has('function_')) {
			return {color: 'yellow'};
		}
		if (names.has('class_')) {
			return {color: 'blueBright'};
		}
		return {};
	}
	if (names.has('hljs-keyword') || names.has('hljs-literal')) {
		return {color: 'blueBright'};
	}
	if (names.has('hljs-built_in')) {
		return {color: 'cyan'};
	}
	if (names.has('hljs-type')) {
		return {color: 'cyan', dim: true};
	}
	if (names.has('hljs-number')) {
		return {color: 'green'};
	}
	if (names.has('hljs-regexp') || names.has('hljs-string')) {
		return {color: 'red'};
	}
	if (names.has('hljs-class') || names.has('hljs-name')) {
		return {color: 'blueBright'};
	}
	if (names.has('hljs-function')) {
		return {color: 'yellow'};
	}
	if (names.has('hljs-comment') || names.has('hljs-doctag')) {
		return {color: 'green'};
	}
	if (names.has('hljs-meta') || names.has('hljs-tag')) {
		return {color: 'grey'};
	}
	if (names.has('hljs-attr')) {
		return {color: 'cyan'};
	}
	if (names.has('hljs-emphasis')) {
		return {italic: true};
	}
	if (names.has('hljs-strong')) {
		return {bold: true};
	}
	if (names.has('hljs-link')) {
		return {underline: true};
	}
	if (names.has('hljs-addition')) {
		return {color: 'green'};
	}
	if (names.has('hljs-deletion')) {
		return {color: 'red'};
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

type StyleResolver = (className: string) => SpanStyle;

function walk(
	node: HastNode,
	inherited: SpanStyle,
	resolve: StyleResolver,
	out: StyledSpan[],
): void {
	if (node.type === 'text') {
		if (node.value) {
			out.push({text: node.value, ...inherited});
		}
		return;
	}
	const className = Array.isArray(node.properties?.className)
		? node.properties.className.filter((value): value is string => typeof value === 'string').join(' ')
		: '';
	const style = className ? {...inherited, ...resolve(className)} : inherited;
	for (const child of node.children ?? []) {
		walk(child, style, resolve, out);
	}
}

function highlightWithStyles(
	code: string,
	language: string | undefined,
	resolve: StyleResolver,
	defaultStyle: SpanStyle,
): StyledSpan[][] {
	const normalized = (language ?? '').trim().toLowerCase().split(/\s+/)[0] ?? '';
	const source = code.replace(/\t/g, '  ');
	const spans: StyledSpan[] = [];

	if (normalized && lowlight.registered(normalized)) {
		const tree = lowlight.highlight(normalized, source) as HastNode;
		walk(tree, defaultStyle, resolve, spans);
	} else if (source) {
		spans.push({text: source, ...defaultStyle});
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
	return highlightWithStyles(
		code,
		language,
		(className) => styleFor(className, theme),
		{color: theme.colors.foreground},
	);
}

/** Highlight code with Claude Code's fixed diff palette. */
export function highlightClaudeToLines(
	code: string,
	language: string | undefined,
): StyledSpan[][] {
	return highlightWithStyles(code, language, claudeStyleFor, {});
}
