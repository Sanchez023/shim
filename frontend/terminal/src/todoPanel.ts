/**
 * Todo/plan panel shape.  The backend owns the value (`settings.todo_panel`,
 * surfaced in every `state` payload); the TUI only renders it and asks for the
 * next shape via `set_ui_preference`.
 *
 * The backend publishes this through the `state` payload, which is UI-only and
 * never part of the provider request — toggling it cannot affect prompt caching.
 */
export const TODO_PANEL_MODES = ['full', 'compact', 'hidden'] as const;

export type TodoPanelMode = (typeof TODO_PANEL_MODES)[number];

export function parseTodoPanel(value: unknown): TodoPanelMode {
	return TODO_PANEL_MODES.includes(value as TodoPanelMode)
		? (value as TodoPanelMode)
		: 'full';
}

export function nextTodoPanel(current: TodoPanelMode): TodoPanelMode {
	const index = TODO_PANEL_MODES.indexOf(current);
	return TODO_PANEL_MODES[(index + 1) % TODO_PANEL_MODES.length] ?? 'full';
}
