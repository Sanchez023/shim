import React, {useDeferredValue, useEffect, useMemo, useRef, useState} from 'react';
import {Box, Static, Text, useApp, useInput, useStdout} from 'ink';

import {readClipboardImage, type ImageAttachment} from './clipboardImage.js';
import {copyTextToClipboard} from './clipboardText.js';
import {CommandPicker} from './components/CommandPicker.js';
import {
	ConversationView,
	TranscriptRow,
	groupToolPairs,
	splitTranscript,
	staticBoundary,
} from './components/ConversationView.js';
import {FilePicker} from './components/FilePicker.js';
import {HelpOverlay} from './components/HelpOverlay.js';
import {HistorySearch} from './components/HistorySearch.js';
import {ModalHost} from './components/ModalHost.js';
import {PromptInput} from './components/PromptInput.js';
import {SelectModal, type SelectOption} from './components/SelectModal.js';
import {StatusBar} from './components/StatusBar.js';
import {SwarmPanel} from './components/SwarmPanel.js';
import {TodoPanel, parseTodoItems} from './components/TodoPanel.js';
import {TranscriptView, buildTranscriptLines} from './components/TranscriptView.js';
import type {TranscriptLine} from './components/TranscriptView.js';
import {useBackendSession} from './hooks/useBackendSession.js';
import {useTerminalSize} from './hooks/useTerminalSize.js';
import {buildTerminalTitle, writeTerminalTitle} from './terminalTitle.js';
import {nextTranscriptOffset} from './transcriptScroll.js';
import {isBusySafeCommand} from './busyCommands.js';
import {nextTodoPanel, parseTodoPanel, type TodoPanelMode} from './todoPanel.js';
import {filterSelectOptions} from './selectFilter.js';
import {ThemeProvider, useTheme} from './theme/ThemeContext.js';
import type {
	FrontendConfig,
	ImageAttachmentPayload,
} from './types.js';

const rawReturnSubmit = process.env.OPENHARNESS_FRONTEND_RAW_RETURN === '1';
const scriptedSteps = (() => {
	const raw = process.env.OPENHARNESS_FRONTEND_SCRIPT;
	if (!raw) {
		return [] as string[];
	}
	try {
		const parsed = JSON.parse(raw);
		return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : [];
	} catch {
		return [];
	}
})();

const SELECTABLE_COMMANDS = new Set([
	'/provider',
	'/model',
	'/theme',
	'/output-style',
	'/permissions',
	'/resume',
	'/effort',
	'/passes',
	'/turns',
	'/fast',
	'/vim',
	'/voice',
]);

type SelectModalState = {
	title: string;
	options: SelectOption[];
	onSelect: (value: string) => void;
	searchable?: boolean;
} | null;

type QueuedMessage = {
	kind: 'submit_line' | 'run_shell';
	line: string;
	// Keep the live attachments so pressing ↑ can pull an image message back
	// into the editor without losing its images.
	attachments: ImageAttachment[];
};

const toImagePayloads = (attachments: ImageAttachment[]): ImageAttachmentPayload[] =>
	attachments.map((image) => ({
		media_type: image.media_type,
		data: image.data,
		source_path: image.source_path,
	}));

function queuedPreview(message: QueuedMessage): string {
	const firstLine = (message.line.split('\n')[0] ?? '').trim();
	const text = (firstLine || '(empty)').slice(0, 120);
	const count = message.attachments.length;
	return count > 0 ? `${text} [${count} image${count === 1 ? '' : 's'}]` : text;
}

function extractLastCodeBlock(text: string): string | null {
	const matches = [...text.matchAll(/```[^\n]*\n([\s\S]*?)```/g)];
	if (matches.length === 0) {
		return null;
	}
	return matches[matches.length - 1][1].replace(/\n$/, '');
}

const COMMAND_USAGE: Record<string, string> = {
	'/model': '/model <alias|model-id>',
	'/provider': '/provider <profile>',
	'/permissions': '/permissions [default|plan|full_auto]',
	'/resume': '/resume [session-id]',
	'/theme': '/theme <name>',
	'/effort': '/effort [off|low|medium|high|xhigh]',
	'/thinking': '/thinking [show [full]|on|off|toggle|probe [on|off]]',
	'/tools': '/tools [show|profile core|full|enable NAME]',
	'/loop': '/loop <interval> <prompt> | /loop status|list|stop [NAME|all]',
	'/notify': '/notify [show|on|off|test]',
	'/turns': '/turns <n>',
	'/passes': '/passes <n>',
	'/output-style': '/output-style <name>',
};

type HistorySearchState = {
	query: string;
	index: number;
} | null;

export function App({config}: {config: FrontendConfig}): React.JSX.Element {
	const initialTheme = String((config as Record<string, unknown>).theme ?? 'default');
	return (
		<ThemeProvider initialTheme={initialTheme}>
			<AppInner config={config} />
		</ThemeProvider>
	);
}

function AppInner({config}: {config: FrontendConfig}): React.JSX.Element {
	const {exit} = useApp();
	const {theme, setThemeName} = useTheme();
	const [input, setInput] = useState('');
	const [modalInput, setModalInput] = useState('');
	const [history, setHistory] = useState<string[]>([]);
	const [historyIndex, setHistoryIndex] = useState(-1);
	const [historySearch, setHistorySearch] = useState<HistorySearchState>(null);
	const [showHelp, setShowHelp] = useState(false);
	const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
	const [pendingChord, setPendingChord] = useState<string | null>(null);
	const chordTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
	const [busyStartedAt, setBusyStartedAt] = useState<number | null>(null);
	const [elapsedSeconds, setElapsedSeconds] = useState(0);
	const [lastTurnSeconds, setLastTurnSeconds] = useState(0);
	const [showTranscript, setShowTranscript] = useState(false);
	const [transcriptScroll, setTranscriptScroll] = useState(0);
	const transcriptScrollRef = useRef(0);
	const [outputView, setOutputView] = useState<{title: string; lines: TranscriptLine[]} | null>(null);
	const [outputScroll, setOutputScroll] = useState(0);
	const outputScrollRef = useRef(0);
	const lastAutoOutputIndexRef = useRef(-1);
	const [filePickerIndex, setFilePickerIndex] = useState(0);
	const [imageAttachments, setImageAttachments] = useState<ImageAttachment[]>([]);
	const [clipboardStatus, setClipboardStatus] = useState<string | null>(null);
	const [lastEscapeAt, setLastEscapeAt] = useState(0);
	const [scriptIndex, setScriptIndex] = useState(0);
	const [pickerIndex, setPickerIndex] = useState(0);
	const [selectModal, setSelectModal] = useState<SelectModalState>(null);
	const [selectIndex, setSelectIndex] = useState(0);
	const [selectQuery, setSelectQuery] = useState('');
	const session = useBackendSession(config, () => exit());
	// Ctrl+T cycles the todo panel. The backend owns the persisted value; a
	// local override keeps rapid taps moving forward while the round-trip is in
	// flight, and is dropped once the backend echoes the same value.
	const backendTodoPanel = parseTodoPanel(session.status.todo_panel);
	const [todoPanelOverride, setTodoPanelOverride] = useState<TodoPanelMode | null>(null);
	const todoDisplay = todoPanelOverride ?? backendTodoPanel;
	useEffect(() => {
		setTodoPanelOverride((current) => (current === backendTodoPanel ? null : current));
	}, [backendTodoPanel]);
	const deferredTranscript = useDeferredValue(session.transcript);

	const clearPendingChord = (): void => {
		if (chordTimerRef.current !== null) {
			clearTimeout(chordTimerRef.current);
			chordTimerRef.current = null;
		}
		setPendingChord(null);
	};

	const armChord = (prefix: string): void => {
		if (chordTimerRef.current !== null) {
			clearTimeout(chordTimerRef.current);
		}
		setPendingChord(prefix);
		chordTimerRef.current = setTimeout(() => {
			chordTimerRef.current = null;
			setPendingChord(null);
		}, 3000);
	};

	useEffect(
		() => () => {
			if (chordTimerRef.current !== null) {
				clearTimeout(chordTimerRef.current);
			}
		},
		[],
	);
	const deferredAssistantBuffer = useDeferredValue(session.assistantBuffer);
	const deferredThinkingBuffer = useDeferredValue(session.thinkingBuffer);
	const deferredTasks = useDeferredValue(session.tasks);
	const deferredTodoMarkdown = useDeferredValue(session.todoMarkdown);
	const deferredSwarmTeammates = useDeferredValue(session.swarmTeammates);
	const deferredSwarmNotifications = useDeferredValue(session.swarmNotifications);
	const {stdout} = useStdout();
	const appName = String(config.app_name ?? 'shim');
	const appVersion = typeof config.version === 'string' ? config.version : undefined;
	const lastTerminalTitleRef = useRef('');
	const waitingForInput = Boolean(session.modal || selectModal);
	// Searchable pickers (e.g. /resume) filter client-side; keep the cursor in
	// range when the query narrows the list.
	const selectVisibleOptions = useMemo(
		() => (selectModal ? filterSelectOptions(selectModal.options, selectQuery) : []),
		[selectModal, selectQuery],
	);
	const selectActiveIndex = Math.min(
		selectIndex,
		Math.max(0, selectVisibleOptions.length - 1),
	);
	const {rows: terminalRows, columns: terminalColumns} = useTerminalSize();
	const planMode = String(session.status.permission_mode ?? '').toLowerCase().includes('plan');
	const sidebarWidth = Math.min(40, Math.max(28, Math.floor(terminalColumns * 0.3)));
	// The checklist belongs in the sidebar whenever there is room. Previously
	// it only rendered there in plan mode, so exiting plan mode dropped a tall
	// inline panel above the input, where it crowded out permission prompts.
	const hasSwarm =
		deferredSwarmTeammates.length > 0 || deferredSwarmNotifications.length > 0;
	const showTodoPanel = Boolean(deferredTodoMarkdown) && todoDisplay !== 'hidden';
	const showSidebar =
		session.ready &&
		(showTodoPanel || hasSwarm) &&
		terminalColumns >= 80 &&
		!showTranscript &&
		!outputView &&
		!session.modal &&
		!selectModal;
	// Ink appends a newline to every frame. A frame as tall as the terminal
	// therefore scrolls one line on every render, which looks like a full-screen
	// flash. Leave a few rows of headroom.
	const frameHeight = Math.max(5, terminalRows - 3);
	// The inline checklist is part of the fixed-height frame, so its rows come
	// out of the conversation area. Cap it to roughly half the frame and tell
	// the transcript how much room is actually left: an unbounded panel (or a
	// budget that ignores it) squeezes the newest rows out of view, which reads
	// as "the last answer disappeared".
	const inlineTodoItems = useMemo(
		() => parseTodoItems(deferredTodoMarkdown),
		[deferredTodoMarkdown],
	);
	// At most a third of the window: the checklist is secondary to the answer, and
	// a panel that eats the frame leaves the newest rows nowhere to go.
	const maxInlineTodoItems = Math.max(1, Math.floor((terminalRows - 12) / 3));
	const inlineTodoVisibleItems = Math.min(inlineTodoItems.length, maxInlineTodoItems);
	const showInlineTodo =
		session.ready &&
		!showSidebar &&
		!session.modal &&
		!selectModal &&
		!showTranscript &&
		!outputView &&
		Boolean(deferredTodoMarkdown);
	const inlineTodoRows = !showInlineTodo
		? 0
		: todoDisplay === 'compact'
			? 1
			: // borders (2) + title + the margin above the panel + "… N more"
				inlineTodoVisibleItems +
				5;
	const transcriptRows = Math.max(4, frameHeight - 4 - inlineTodoRows);
	const transcriptLines = useMemo(
		() => buildTranscriptLines(deferredTranscript, deferredAssistantBuffer),
		[deferredTranscript, deferredAssistantBuffer],
	);
	const maxTranscriptScroll = Math.max(0, transcriptLines.length - transcriptRows);
	// Once a turn finishes, its tail is flushed into scrollback (see
	// `printedCount` below). Scrollback has no height budget, so the finished
	// answer is printed in full instead of being folded into "… N more lines"
	// that has to be reopened with ctrl+o. The counter only grows, so an item is
	// never pulled back out of `<Static>` and printed twice.
	const [printedCount, setPrintedCount] = useState(0);
	// Queued follow-ups are handed to the backend at the next tool boundary so
	// a correction reaches the model before its next request, instead of
	// waiting for the whole (possibly many-step) turn to finish.
	const flushedCompletionRef = useRef(0);
	useEffect(() => {
		if (!session.ready || !session.busy || session.modal || selectModal) {
			return;
		}
		if (session.toolCompletions <= flushedCompletionRef.current) {
			return;
		}
		flushedCompletionRef.current = session.toolCompletions;
		if (queuedMessages.length === 0) {
			return;
		}
		const [next, ...rest] = queuedMessages;
		// Shell lines are not prompts; leave them for the turn-end drain.
		if (next.kind === 'run_shell' || next.line.trim().startsWith('/')) {
			return;
		}
		setQueuedMessages(rest);
		session.sendRequest({type: 'submit_line', line: next.line});
	}, [
		queuedMessages,
		selectModal,
		session.busy,
		session.modal,
		session.ready,
		session.toolCompletions,
	]);

	const {staticItems, dynamicItems, printedBoundary} = useMemo(() => {
		const total = deferredTranscript.length;
		const boundary = staticBoundary(total, printedCount);
		const split = splitTranscript(deferredTranscript, total - boundary);
		return {...split, printedBoundary: boundary};
	}, [deferredTranscript, printedCount]);
	// The frame is a fixed `rows - 3` block, so anything that has already been
	// printed into `<Static>` sits above it and only its last few rows stay
	// visible. Flushing the whole transcript on completion therefore pushed the
	// finished answer out of view. Keep the newest items in the frame instead
	// (they are what the user is reading) and only advance the counter, which
	// must stay monotonic: an item pulled back out of `<Static>` would print
	// twice.
	useEffect(() => {
		if (printedBoundary > printedCount) {
			setPrintedCount(printedBoundary);
		}
	}, [printedBoundary, printedCount]);
	const staticGroups = useMemo(() => groupToolPairs(staticItems), [staticItems]);
	const clipboardStatusTimerRef = useRef<NodeJS.Timeout | null>(null);

	// Commands such as /context return more text than fits on screen. Dumping
	// that into the conversation puts one giant block into Ink's <Static>
	// output and leaves the live frame mangled (which reads as if the TUI
	// exited). Open oversized command output in a scrollable view that closes
	// with esc/q/ctrl+o instead, and keep a bounded preview in the transcript.
	useEffect(() => {
		if (deferredTranscript.length === 0) {
			lastAutoOutputIndexRef.current = -1;
			return;
		}
		const index = deferredTranscript.length - 1;
		if (index <= lastAutoOutputIndexRef.current) {
			return;
		}
		lastAutoOutputIndexRef.current = index;
		const item = deferredTranscript[index];
		if (!item || (item.role !== 'system' && item.role !== 'log')) {
			return;
		}
		const lines = buildTranscriptLines([item]);
		if (lines.length <= transcriptRows) {
			return;
		}
		outputScrollRef.current = 0;
		setOutputScroll(0);
		setOutputView({
			title: item.role === 'system' ? 'Command output' : 'Output',
			lines,
		});
	}, [deferredTranscript, transcriptRows]);

	useEffect(() => {
		const nextTheme = session.status.theme;
		if (typeof nextTheme === 'string' && nextTheme) {
			setThemeName(nextTheme);
		}
	}, [session.status.theme, setThemeName]);

	useEffect(() => {
		const title = buildTerminalTitle({
			appName,
			ready: session.ready,
			busy: session.busy,
			busyLabel: session.busyLabel,
			waitingForInput,
			model: String(session.status.model ?? ''),
			queuedCount: queuedMessages.length,
		});
		if (title === lastTerminalTitleRef.current) {
			return;
		}
		lastTerminalTitleRef.current = title;
		writeTerminalTitle(stdout, title);
	}, [
		appName,
		session.ready,
		session.busy,
		session.busyLabel,
		session.status.model,
		waitingForInput,
		queuedMessages.length,
		stdout,
	]);

	useEffect(() => {
		return () => {
			if (clipboardStatusTimerRef.current) {
				clearTimeout(clipboardStatusTimerRef.current);
			}
		};
	}, []);

	const setTemporaryClipboardStatus = (message: string): void => {
		setClipboardStatus(message);
		if (clipboardStatusTimerRef.current) {
			clearTimeout(clipboardStatusTimerRef.current);
		}
		clipboardStatusTimerRef.current = setTimeout(() => {
			setClipboardStatus(null);
			clipboardStatusTimerRef.current = null;
		}, 2500);
	};

	const attachClipboardImage = (): void => {
		void (async () => {
			const image = await readClipboardImage();
			if (!image) {
				setTemporaryClipboardStatus('No image found in clipboard');
				return;
			}
			setImageAttachments((items) => [...items, image]);
			setTemporaryClipboardStatus(`Attached ${image.label}`);
		})().catch((error: unknown) => {
			const message = error instanceof Error ? error.message : String(error);
			setTemporaryClipboardStatus(`Clipboard image unavailable: ${message}`);
		});
	};

	// Previews for the queued messages shown under the prompt. Keeping the
	// text visible is what makes "type while the agent is running" discoverable.
	const queuedPreviews = queuedMessages.map(queuedPreview);

	// Current tool name for spinner
	const currentToolName = useMemo(() => {
		for (let i = deferredTranscript.length - 1; i >= 0; i--) {
			const item = deferredTranscript[i];
			if (item.role === 'tool') {
				return item.tool_name ?? 'tool';
			}
			if (item.role === 'tool_result' || item.role === 'assistant') {
				break;
			}
		}
		return undefined;
	}, [deferredTranscript]);

	// Command hints
	const commandHints = useMemo(() => {
		const value = input.trim();
		if (!value.startsWith('/')) {
			return [] as string[];
		}
		return session.commands.filter((cmd) => cmd.startsWith(value)).slice(0, 10);
	}, [session.commands, input]);

	// The command picker and history stay available while the agent is running:
	// submitting from them queues the line just like a typed message.
	const showPicker = commandHints.length > 0 && !session.modal && !selectModal;
	const commandQuery = input.trim().startsWith('/') ? input.trim().slice(1) : null;
	const showCommandEmpty =
		commandHints.length === 0 &&
		commandQuery !== null &&
		commandQuery.length > 0 &&
		!session.modal &&
		!selectModal &&
		!historySearch &&
		!showHelp &&
		!showTranscript &&
		!outputView;
	const outputStyle = String(session.status.output_style ?? 'default');
	const historyQuery = historySearch?.query ?? '';
	const historyMatches = useMemo(() => {
		const needle = historyQuery.trim().toLowerCase();
		const seen = new Set<string>();
		return [...history]
			.reverse()
			.filter((item) => {
				if (seen.has(item)) {
					return false;
				}
				seen.add(item);
				return needle === '' || item.toLowerCase().includes(needle);
			})
			.slice(0, 20);
	}, [history, historyQuery]);

	const mentionMatch = /(?:^|\s)@([^\s@]*)$/.exec(input);
	const mentionQuery = mentionMatch ? mentionMatch[1] : null;
	const mentionStart = mentionQuery !== null ? input.lastIndexOf('@') : -1;
	const showFilePicker =
		mentionQuery !== null &&
		session.fileMatches.length > 0 &&
		!session.busy &&
		!session.modal &&
		!selectModal &&
		!historySearch &&
		!showHelp;
	const showFileEmpty =
		mentionQuery !== null &&
		mentionQuery.length > 0 &&
		session.fileMatches.length === 0 &&
		!session.busy &&
		!session.modal &&
		!selectModal &&
		!historySearch &&
		!showHelp &&
		!showTranscript &&
		!outputView;
	const requestFilesRef = useRef(session.requestFiles);
	requestFilesRef.current = session.requestFiles;

	useEffect(() => {
		setFilePickerIndex(0);
	}, [mentionQuery]);

	useEffect(() => {
		setFilePickerIndex((index) =>
			Math.min(index, Math.max(0, session.fileMatches.length - 1)),
		);
	}, [session.fileMatches.length]);

	useEffect(() => {
		if (mentionQuery === null || session.busy) {
			return;
		}
		const timer = setTimeout(() => {
			requestFilesRef.current(mentionQuery);
		}, 120);
		return () => clearTimeout(timer);
	}, [mentionQuery, session.busy]);

	const wasBusyRef = useRef(false);
	const elapsedRef = useRef(0);
	elapsedRef.current = elapsedSeconds;
	useEffect(() => {
		if (wasBusyRef.current && !session.busy) {
			setLastTurnSeconds(elapsedRef.current);
		}
		wasBusyRef.current = session.busy;
	}, [session.busy]);

	// When a turn finishes, send the next queued message automatically.
	useEffect(() => {
		if (!session.ready || session.busy || session.modal || selectModal) {
			return;
		}
		if (queuedMessages.length === 0) {
			return;
		}
		const [next, ...rest] = queuedMessages;
		setQueuedMessages(rest);
		session.sendRequest(
			next.kind === 'run_shell'
				? {type: 'run_shell', line: next.line}
				: {type: 'submit_line', line: next.line, images: toImagePayloads(next.attachments)},
		);
		session.setBusy(true);
		session.setBusyLabel(undefined);
	}, [queuedMessages, session.busy, session.ready, session.modal, selectModal]);

	useEffect(() => {
		if (session.busy) {
			setBusyStartedAt((previous) => previous ?? Date.now());
			return;
		}
		setBusyStartedAt(null);
		setElapsedSeconds(0);
	}, [session.busy]);

	useEffect(() => {
		if (!session.busy || busyStartedAt === null) {
			return;
		}
		const timer = setInterval(() => {
			setElapsedSeconds(Math.max(0, Math.floor((Date.now() - busyStartedAt) / 1000)));
		}, 1000);
		return () => clearInterval(timer);
	}, [session.busy, busyStartedAt]);

	useEffect(() => {
		setPickerIndex(0);
	}, [commandHints.length, input]);

	// Handle backend-initiated select requests (e.g. /resume session list)
	useEffect(() => {
		if (!session.selectRequest) {
			return;
		}
		const req = session.selectRequest;
		if (req.options.length === 0) {
			session.setSelectRequest(null);
			return;
		}
		const initialIndex = req.options.findIndex((option) => option.active);
		setSelectIndex(initialIndex >= 0 ? initialIndex : 0);
		setSelectQuery('');
		setSelectModal({
			title: req.title,
			options: req.options.map((o) => ({value: o.value, label: o.label, description: o.description, active: o.active})),
			searchable: req.searchable === true,
			onSelect: (value) => {
				session.sendRequest({type: 'apply_select_command', command: req.command, value});
				session.setBusy(true);
				setSelectModal(null);
				setSelectQuery('');
			},
		});
		session.setSelectRequest(null);
	}, [session.selectRequest]);

	// `shim --resume` (no value) opens the searchable session picker inside the
	// TUI instead of prompting on the terminal before launch.
	const initialModalOpened = useRef(false);
	useEffect(() => {
		if (initialModalOpened.current || !session.ready) {
			return;
		}
		initialModalOpened.current = true;
		if (config.initial_modal === 'resume') {
			session.sendRequest({type: 'select_command', command: 'resume'});
		}
	}, [config.initial_modal, session.ready, session.sendRequest]);

	// Intercept special commands that need interactive UI
	const handleCommand = (cmd: string): boolean => {
		const trimmed = cmd.trim();

		if (SELECTABLE_COMMANDS.has(trimmed)) {
			session.sendRequest({type: 'select_command', command: trimmed.slice(1)});
			return true;
		}

		// /permissions → show mode picker
		if (trimmed === '/permissions' || trimmed === '/permissions show') {
			session.sendRequest({type: 'select_command', command: 'permissions'});
			return true;
		}

		// /plan → toggle plan mode. Status values arrive either raw ("plan")
		// or formatted ("Plan Mode"), so compare on the normalized form.
		if (trimmed === '/plan') {
			const currentMode = String(session.status.permission_mode ?? 'default').toLowerCase();
			if (currentMode.includes('plan')) {
				session.sendRequest({type: 'submit_line', line: '/plan off'});
			} else {
				session.sendRequest({type: 'submit_line', line: '/plan on'});
			}
			session.setBusy(true);
			return true;
		}

		// /resume → request session list from backend (will trigger select_request)
		if (trimmed === '/resume') {
			session.sendRequest({type: 'select_command', command: 'resume'});
			return true;
		}

		return false;
	};

	useInput((chunk, key) => {
		const isPaste = chunk.length > 1 && !key.ctrl && !key.meta;
		const isEscape = key.escape || chunk === '\u001B';

		// The transcript viewer is a separate screen (mirrors Claude Code's
		// app:toggleTranscript). Ctrl+C / Ctrl+O / q / Esc close it rather than
		// interrupting the turn or exiting the app.
		const scrollTranscript = (delta: number): void => {
			const previous = transcriptScrollRef.current;
			const next = nextTranscriptOffset(previous, maxTranscriptScroll, delta);
			if (next === null) {
				return;
			}
			transcriptScrollRef.current = next;
			setTranscriptScroll(next);
		};

		if (showTranscript) {
			if ((key.ctrl && (chunk === 'c' || chunk === 'o')) || isEscape || chunk === 'q') {
				transcriptScrollRef.current = 0;
				setShowTranscript(false);
				return;
			}
			if (key.upArrow || chunk === 'k') {
				scrollTranscript(1);
				return;
			}
			if (key.downArrow || chunk === 'j') {
				scrollTranscript(-1);
				return;
			}
			if (key.pageUp || (key.ctrl && chunk === 'u')) {
				scrollTranscript(transcriptRows);
				return;
			}
			if (key.pageDown || (key.ctrl && chunk === 'd')) {
				scrollTranscript(-transcriptRows);
				return;
			}
			if (chunk === 'g') {
				scrollTranscript(maxTranscriptScroll);
				return;
			}
			if (chunk === 'G') {
				scrollTranscript(-maxTranscriptScroll);
				return;
			}
			return;
		}

		// Oversized command output (/context, /help, stack traces) opens in its
		// own scrollable view. Closing it returns to the conversation, where the
		// transcript keeps a bounded preview plus a ctrl+o hint.
		if (outputView && !session.modal && !selectModal) {
			const maxOutputScroll = Math.max(0, outputView.lines.length - transcriptRows);
			const scrollOutput = (delta: number): void => {
				const next = nextTranscriptOffset(outputScrollRef.current, maxOutputScroll, delta);
				if (next === null) {
					return;
				}
				outputScrollRef.current = next;
				setOutputScroll(next);
			};

			if ((key.ctrl && (chunk === 'c' || chunk === 'o')) || isEscape || chunk === 'q') {
				outputScrollRef.current = 0;
				setOutputScroll(0);
				setOutputView(null);
				return;
			}
			if (key.upArrow || chunk === 'k') {
				scrollOutput(1);
				return;
			}
			if (key.downArrow || chunk === 'j') {
				scrollOutput(-1);
				return;
			}
			if (key.pageUp || (key.ctrl && chunk === 'u')) {
				scrollOutput(transcriptRows);
				return;
			}
			if (key.pageDown || (key.ctrl && chunk === 'd')) {
				scrollOutput(-transcriptRows);
				return;
			}
			if (chunk === 'g') {
				scrollOutput(maxOutputScroll);
				return;
			}
			if (chunk === 'G') {
				scrollOutput(-maxOutputScroll);
				return;
			}
			return;
		}

		// Ctrl+C interrupts a running turn; when idle it exits the TUI.
		if (key.ctrl && chunk === 'c') {
			if (session.busy) {
				session.sendRequest({type: 'interrupt'});
				session.setBusyLabel('Stopping current operation...');
				return;
			}
			session.sendRequest({type: 'shutdown'});
			exit();
			return;
		}

		// Ctrl+R opens reverse history search.
		if (
			!session.busy &&
			!session.modal &&
			!selectModal &&
			!historySearch &&
			!showHelp &&
			key.ctrl &&
			chunk === 'r'
		) {
			setHistorySearch({query: '', index: 0});
			setShowHelp(false);
			return;
		}

		if (historySearch) {
			if (isEscape) {
				setHistorySearch(null);
				return;
			}
			if (key.ctrl && chunk === 'r' && historyMatches.length > 0) {
				setHistorySearch((current) =>
					current === null
						? current
						: {
								...current,
								index: (current.index + 1) % historyMatches.length,
							},
				);
				return;
			}
			if (key.upArrow) {
				setHistorySearch((current) =>
					current === null
						? current
						: {...current, index: Math.max(0, current.index - 1)},
				);
				return;
			}
			if (key.downArrow) {
				setHistorySearch((current) =>
					current === null
						? current
						: {
								...current,
								index: Math.min(
									Math.max(0, historyMatches.length - 1),
									current.index + 1,
								),
							},
				);
				return;
			}
			if (key.return) {
				const selected = historyMatches[historySearch.index];
				if (selected !== undefined) {
					setInput(selected);
				}
				setHistorySearch(null);
				return;
			}
			if (key.backspace) {
				setHistorySearch((current) =>
					current === null
						? current
						: {...current, query: current.query.slice(0, -1), index: 0},
				);
				return;
			}
			if (chunk && !key.ctrl && !key.meta && !key.return) {
				setHistorySearch((current) =>
					current === null
						? current
						: {...current, query: current.query + chunk, index: 0},
				);
			}
			return;
		}

		if (showHelp) {
			if (isEscape || chunk === '?') {
				setShowHelp(false);
			}
			return;
		}

		// Claude Code-style queue editing: ↑ on an empty prompt pulls the most
		// recently queued message back into the editor so it can be changed or
		// dropped before the running turn finishes.
		if (
			session.busy &&
			key.upArrow &&
			!showPicker &&
			!showFilePicker &&
			input === '' &&
			queuedMessages.length > 0
		) {
			const last = queuedMessages[queuedMessages.length - 1];
			setQueuedMessages(queuedMessages.slice(0, -1));
			setInput(last.line);
			setImageAttachments(last.attachments);
			setHistoryIndex(-1);
			return;
		}

		if (
			!session.busy &&
			!session.modal &&
			!selectModal &&
			chunk === '?' &&
			input.trim() === '' &&
			imageAttachments.length === 0
		) {
			setShowHelp(true);
			return;
		}

		if (showFilePicker) {
			if (key.upArrow) {
				setFilePickerIndex((index) => Math.max(0, index - 1));
				return;
			}
			if (key.downArrow) {
				setFilePickerIndex((index) =>
					Math.min(session.fileMatches.length - 1, index + 1),
				);
				return;
			}
			if (key.return || key.tab) {
				const selected = session.fileMatches[filePickerIndex];
				if (selected) {
					setInput(`${input.slice(0, mentionStart)}@${selected} `);
				}
				return;
			}
			if (isEscape) {
				setInput(input.slice(0, mentionStart));
				return;
			}
		}

		if (key.ctrl && chunk === 'o') {
			transcriptScrollRef.current = 0;
			setTranscriptScroll(0);
			setShowTranscript(true);
			return;
		}

		if (key.ctrl && chunk === 'y') {
			const lastAssistant = [...session.transcript]
				.reverse()
				.find((item) => item.role === 'assistant');
			if (lastAssistant) {
				const codeBlock = extractLastCodeBlock(lastAssistant.text);
				const copied = copyTextToClipboard(codeBlock ?? lastAssistant.text);
				setTemporaryClipboardStatus(
					copied
						? codeBlock
							? 'Copied last code block'
							: 'Copied last assistant message'
						: 'Clipboard unavailable',
				);
			}
			return;
		}

		if (key.ctrl && chunk === 't') {
			const next = nextTodoPanel(todoDisplay);
			setTodoPanelOverride(next);
			session.sendRequest({
				type: 'set_ui_preference',
				key: 'todo_panel',
				value: next,
			});
			return;
		}

		if (key.ctrl && chunk === 'b') {
			if (session.busy) {
				session.sendRequest({type: 'background_task'});
			}
			return;
		}

		if (key.ctrl && chunk === 'v') {
			attachClipboardImage();
			return;
		}

		// Let ink-text-input handle pasted text directly.
		if (isPaste) {
			return;
		}

		// --- Select modal (permissions picker etc.) ---
		if (selectModal) {
			if (key.upArrow) {
				setSelectIndex((i) => Math.max(0, Math.min(i, selectVisibleOptions.length - 1) - 1));
				return;
			}
			if (key.downArrow) {
				setSelectIndex((i) =>
					Math.min(Math.max(0, selectVisibleOptions.length - 1), i + 1),
				);
				return;
			}
			if (key.return) {
				const selected = selectVisibleOptions[selectActiveIndex];
				if (selected) {
					selectModal.onSelect(selected.value);
				}
				return;
			}
			if (key.escape) {
				setSelectModal(null);
				setSelectQuery('');
				return;
			}
			if (selectModal.searchable) {
				if (key.backspace || key.delete) {
					setSelectQuery((current) => current.slice(0, -1));
					setSelectIndex(0);
					return;
				}
				if (chunk && !key.ctrl && !key.meta && !key.tab) {
					setSelectQuery((current) => current + chunk);
					setSelectIndex(0);
					return;
				}
			}
			// Number keys for quick selection (only while no search query is typed)
			const num = parseInt(chunk, 10);
			if (!selectQuery && num >= 1 && num <= selectVisibleOptions.length) {
				const selected = selectVisibleOptions[num - 1];
				if (selected) {
					selectModal.onSelect(selected.value);
				}
				return;
			}
			return;
		}

		// --- Scripted raw return ---
		if (rawReturnSubmit && key.return) {
			if (session.modal?.kind === 'question') {
				session.sendRequest({
					type: 'question_response',
					request_id: session.modal.request_id,
					answer: modalInput,
				});
				session.setModal(null);
				setModalInput('');
				return;
			}
			if (!session.modal && !session.busy && input.trim()) {
				onSubmit(input);
				return;
			}
		}

		// --- Permission modal (MUST be before busy check — modal appears while busy) ---
		if (session.modal?.kind === 'permission') {
			if (chunk.toLowerCase() === 'y') {
				session.sendRequest({
					type: 'permission_response',
					request_id: session.modal.request_id,
					allowed: true,
				});
				session.setModal(null);
				return;
			}
			if (chunk.toLowerCase() === 'n' || isEscape) {
				session.sendRequest({
					type: 'permission_response',
					request_id: session.modal.request_id,
					allowed: false,
				});
				session.setModal(null);
				return;
			}
			return;
		}

		// --- Edit diff modal (also appears while busy) ---
		if (session.modal?.kind === 'edit_diff') {
			if (chunk.toLowerCase() === 'y') {
				session.sendRequest({
					type: 'permission_response',
					request_id: session.modal.request_id,
					allowed: true,
					permission_reply: 'once',
				});
				session.setModal(null);
				return;
			}
			if (chunk.toLowerCase() === 'a') {
				session.sendRequest({
					type: 'permission_response',
					request_id: session.modal.request_id,
					allowed: true,
					permission_reply: 'always',
				});
				session.setModal(null);
				return;
			}
			if (chunk.toLowerCase() === 'n' || isEscape) {
				session.sendRequest({
					type: 'permission_response',
					request_id: session.modal.request_id,
					allowed: false,
					permission_reply: 'reject',
				});
				session.setModal(null);
				return;
			}
			return;
		}

		// --- Question modal (also appears while busy) ---
		if (session.modal?.kind === 'question') {
			return; // Let TextInput in ModalHost handle input
		}

		// Claude Code-style chord: ctrl+x then ctrl+s sends the queued input
		// immediately, interrupting the running turn instead of waiting.
		if (pendingChord !== null) {
			const prefix = pendingChord;
			clearPendingChord();
			if (prefix === 'ctrl+x' && key.ctrl && chunk === 's') {
				sendNow();
				return;
			}
			// Any other second key cancels the chord and falls through normally.
		}

		if (
			key.ctrl &&
			chunk === 'x' &&
			!showPicker &&
			!showFilePicker &&
			!selectModal
		) {
			armChord('ctrl+x');
			return;
		}

		// Esc closes the command picker first; only then does it interrupt the
		// running turn. Otherwise a half-typed "/" would stop the agent.
		if (session.busy && isEscape && !showPicker) {
			session.sendRequest({type: 'interrupt'});
			session.setBusyLabel('Stopping current operation...');
			return;
		}

		// Empty-input Tab opens the permission mode picker. This makes leaving
		// plan mode explicit without requiring users to remember /permissions.
		// It is disabled mid-turn because a mode change would race the agent.
		if (!session.busy && !showPicker && key.tab && input.trim() === '') {
			session.sendRequest({type: 'select_command', command: 'permissions'});
			return;
		}

		// --- Command picker ---
		if (showPicker) {
			if (key.upArrow) {
				setPickerIndex((i) => Math.max(0, i - 1));
				return;
			}
			if (key.downArrow) {
				setPickerIndex((i) => Math.min(commandHints.length - 1, i + 1));
				return;
			}
			if (key.return) {
				const selected = commandHints[pickerIndex];
				if (selected) {
					setInput('');
					// While a turn is running, queue the line instead of applying
					// it immediately — the backend is busy and would reject it.
					if (session.busy || !handleCommand(selected)) {
						onSubmit(selected);
					}
				}
				return;
			}
			if (key.tab) {
				const selected = commandHints[pickerIndex];
				if (selected) {
					// Complete to the selected command with no trailing space —
					// the user can hit Enter immediately to run it, or keep
					// typing to add args. The trailing space made it look like
					// Tab was "committing" with a token, which broke the flow.
					setInput(selected);
				}
				return;
			}
			if (isEscape) {
				setInput('');
				return;
			}
		}

		if (isEscape) {
			const now = Date.now();
			if ((input || imageAttachments.length > 0) && now - lastEscapeAt < 500) {
				setInput('');
				setImageAttachments([]);
				setHistoryIndex(-1);
				setLastEscapeAt(0);
				return;
			}
			setLastEscapeAt(now);
			return;
		}

		// --- History navigation ---
		if (!showPicker && key.upArrow) {
			const nextIndex = Math.min(history.length - 1, historyIndex + 1);
			if (nextIndex >= 0) {
				setHistoryIndex(nextIndex);
				setInput(history[history.length - 1 - nextIndex] ?? '');
			}
			return;
		}
		if (!showPicker && key.downArrow) {
			const nextIndex = Math.max(-1, historyIndex - 1);
			setHistoryIndex(nextIndex);
			setInput(nextIndex === -1 ? '' : (history[history.length - 1 - nextIndex] ?? ''));
			return;
		}

		// Note: normal Enter submission is handled by TextInput's onSubmit in
		// PromptInput.  Do NOT duplicate it here — that causes double requests.
	});

	const onSubmit = (value: string): void => {
		if (session.modal?.kind === 'question') {
			session.sendRequest({
				type: 'question_response',
				request_id: session.modal.request_id,
				answer: value,
			});
			session.setModal(null);
			setModalInput('');
			return;
		}
		if (!session.ready) {
			return;
		}
		if (session.busy) {
			if (value.trim() === '/stop') {
				session.sendRequest({type: 'interrupt'});
				session.setBusyLabel('Stopping current operation...');
				setInput('');
				return;
			}
			// Read-only commands (status/cost/diff/...) do not need to wait for
			// the running turn; the backend executes them in place.
			if (
				imageAttachments.length === 0 &&
				isBusySafeCommand(value, session.busySafeCommands)
			) {
				session.sendRequest({type: 'submit_line', line: value.trim()});
				if (value.trim()) {
					setHistory((items) => [...items, value]);
				}
				setHistoryIndex(-1);
				setInput('');
				return;
			}
			if (value.trim() || imageAttachments.length > 0) {
				const shell = value.trim().startsWith('!');
				setQueuedMessages((items) => [
					...items,
					{
						kind: shell ? 'run_shell' : 'submit_line',
						line: value,
						attachments: shell ? [] : imageAttachments,
					},
				]);
				if (value.trim()) {
					setHistory((items) => [...items, value]);
				}
				setHistoryIndex(-1);
				setInput('');
				setImageAttachments([]);
				setTemporaryClipboardStatus(
					`Queued message (${queuedMessages.length + 1})`,
				);
			}
			return;
		}
		if (!value.trim() && imageAttachments.length === 0) {
			return;
		}
		if (value.trim().startsWith('!') && imageAttachments.length === 0) {
			session.sendRequest({type: 'run_shell', line: value.trim()});
			setHistory((items) => [...items, value]);
			setHistoryIndex(-1);
			setInput('');
			session.setBusy(true);
			return;
		}
		// Check if it's an interactive command
		if (imageAttachments.length === 0 && handleCommand(value)) {
			setHistory((items) => [...items, value]);
			setHistoryIndex(-1);
			setInput('');
			return;
		}
		session.sendRequest({type: 'submit_line', line: value, images: toImagePayloads(imageAttachments)});
		if (value.trim()) {
			setHistory((items) => [...items, value]);
		}
		setHistoryIndex(-1);
		setInput('');
		setImageAttachments([]);
		session.setBusy(true);
	};

	// ctrl+x ctrl+s: interrupt the running turn and send the next pending
	// message immediately instead of waiting for the queue to drain. The
	// backend runs one turn at a time, so anything after the first message
	// stays queued and is submitted by the auto-drain effect.
	const sendNow = (): void => {
		if (!session.ready) {
			return;
		}

		const currentHasContent = input.trim() !== '' || imageAttachments.length > 0;
		if (!session.busy) {
			if (currentHasContent) {
				onSubmit(input);
			}
			return;
		}

		const pending: QueuedMessage[] = [...queuedMessages];
		if (currentHasContent) {
			const shell = input.trim().startsWith('!');
			pending.push({
				kind: shell ? 'run_shell' : 'submit_line',
				line: input,
				attachments: shell ? [] : imageAttachments,
			});
		}
		if (pending.length === 0) {
			return;
		}

		if (input.trim()) {
			setHistory((items) => [...items, input]);
		}
		setHistoryIndex(-1);
		setInput('');
		setImageAttachments([]);

		session.sendRequest({type: 'interrupt'});

		const [next, ...rest] = pending;
		// Slash commands are dispatched locally by onSubmit; keep them queued so
		// the auto-drain effect runs them once the interrupt settles. Sending a
		// slash command to the backend would treat it as a prompt.
		const isLocalCommand =
			next.kind === 'submit_line' && next.line.trim().startsWith('/');
		if (isLocalCommand) {
			setQueuedMessages(pending);
			return;
		}

		setQueuedMessages(rest);
		session.sendRequest(
			next.kind === 'run_shell'
				? {type: 'run_shell', line: next.line}
				: {type: 'submit_line', line: next.line, images: toImagePayloads(next.attachments)},
		);
		session.setBusy(true);
		session.setBusyLabel(undefined);
	};

	// Scripted automation
	useEffect(() => {
		if (scriptIndex >= scriptedSteps.length) {
			return;
		}
		if (session.busy || session.modal || selectModal) {
			return;
		}
		const step = scriptedSteps[scriptIndex];
		const timer = setTimeout(() => {
			onSubmit(step);
			setScriptIndex((index) => index + 1);
		}, 200);
		return () => clearTimeout(timer);
	}, [scriptIndex, session.busy, session.modal, selectModal]);

	return (
		<Box flexDirection="column" paddingX={1} height={frameHeight}>
			<Static
				key={staticItems.length === 0 ? 'static-empty' : 'static-filled'}
				items={staticGroups}
			>
				{(group, index) => (
					<TranscriptRow
						key={index}
						group={group}
						theme={theme}
						outputStyle={outputStyle}
						clamp={false}
					/>
				)}
			</Static>

			<Box flexDirection="row" flexGrow={1} flexBasis={0} overflowY="hidden">
				<Box flexDirection="column" flexGrow={1} overflowY="hidden">
			{/* Conversation area */}
			<Box flexDirection="column" flexGrow={1} flexBasis={0} overflowY="hidden">
				{showTranscript ? (
					<TranscriptView
						lines={transcriptLines}
						height={transcriptRows}
						offset={transcriptScroll}
					/>
				) : outputView ? (
					<TranscriptView
						title={outputView.title}
						lines={outputView.lines}
						height={transcriptRows}
						offset={outputScroll}
					/>
				) : (
					<ConversationView
						items={dynamicItems}
						assistantBuffer={deferredAssistantBuffer}
						thinkingBuffer={deferredThinkingBuffer}
						showWelcome={session.ready && outputStyle !== 'codex' && deferredTranscript.length === 0}
						outputStyle={outputStyle}
						appName={appName}
						version={appVersion}
						maxRows={transcriptRows}
					/>
				)}
			</Box>

			{showHelp && !showTranscript && !outputView ? <HelpOverlay /> : null}

			{historySearch && !showTranscript && !outputView ? (
				<HistorySearch
					query={historySearch.query}
					matches={historyMatches}
					selectedIndex={historySearch.index}
				/>
			) : null}

			{/* Backend modal (permission confirm, question, mcp auth) */}
			{session.modal && !showTranscript ? (
				<ModalHost
					modal={session.modal}
					modalInput={modalInput}
					setModalInput={setModalInput}
					onSubmit={onSubmit}
				/>
			) : null}

			{/* Frontend select modal (permissions picker, etc.) */}
			{selectModal && !showTranscript ? (
				<SelectModal
					title={selectModal.title}
					options={selectVisibleOptions}
					selectedIndex={selectActiveIndex}
					searchable={selectModal.searchable}
					query={selectQuery}
					totalCount={selectModal.options.length}
				/>
			) : null}

			{/* Command picker (including the no-match state) */}
			{(showPicker || showCommandEmpty) && !showTranscript && !outputView ? (
				<CommandPicker
					hints={commandHints}
					selectedIndex={pickerIndex}
					descriptions={session.commandDescriptions}
					usage={COMMAND_USAGE}
					query={showCommandEmpty ? commandQuery ?? '' : undefined}
				/>
			) : null}

			{/* @-mention file picker (including the no-match state) */}
			{(showFilePicker || showFileEmpty) && !showTranscript && !outputView ? (
				<FilePicker
					query={mentionQuery ?? ''}
					matches={session.fileMatches}
					selectedIndex={filePickerIndex}
				/>
			) : null}

			{/* Todo panel (inline only when the sidebar is unavailable) */}
			{showInlineTodo ? (
				<TodoPanel
					markdown={deferredTodoMarkdown}
					display={todoDisplay}
					maxItems={maxInlineTodoItems}
				/>
			) : null}

			{/* Swarm panel */}
			{session.ready && !showSidebar && !session.modal && !selectModal && !showTranscript && !outputView && hasSwarm ? (
				<SwarmPanel teammates={deferredSwarmTeammates} notifications={deferredSwarmNotifications} />
			) : null}

				</Box>
				{showSidebar ? (
					<Box width={sidebarWidth} marginLeft={1} flexDirection="column">
						{showTodoPanel ? (
							<TodoPanel
								markdown={deferredTodoMarkdown}
								display={todoDisplay}
								variant="sidebar"
								title={planMode ? 'Plan' : 'Todo'}
								width={sidebarWidth}
								maxItems={maxInlineTodoItems}
							/>
						) : null}
						{hasSwarm ? (
							<SwarmPanel
								teammates={deferredSwarmTeammates}
								notifications={deferredSwarmNotifications}
							/>
						) : null}
					</Box>
				) : null}
			</Box>

			{/* Input — show loading indicator until backend is ready */}
			{!session.ready ? (
				<Box>
					<Text color={theme.colors.warning}>Connecting to backend...</Text>
				</Box>
			) : session.modal || selectModal || showHelp || historySearch ? null : (
				<PromptInput
					busy={session.busy}
					input={input}
					setInput={setInput}
					onSubmit={onSubmit}
					toolName={session.busy ? currentToolName : undefined}
					statusLabel={session.busy ? (session.busyLabel ?? (currentToolName ? `Running ${currentToolName}...` : 'Running agent loop...')) : undefined}
					suppressSubmit={showPicker || showFilePicker}
					imageAttachmentLabels={imageAttachments.map((image) => image.label)}
					clipboardStatus={clipboardStatus}
					queuedMessages={queuedPreviews}
					readOnly={showTranscript || Boolean(outputView)}
					todoPanelHidden={todoDisplay === 'hidden'}
				/>
			)}

			{/* Status bar sits below the input so it never collides with it */}
			{session.ready ? (
				<StatusBar
					status={session.status}
					tasks={deferredTasks}
					activeToolName={session.busy ? currentToolName : undefined}
					queuedCount={queuedMessages.length}
					busy={session.busy}
					elapsedSeconds={elapsedSeconds}
					lastTurnSeconds={lastTurnSeconds}
				/>
			) : null}

		</Box>
	);
}
