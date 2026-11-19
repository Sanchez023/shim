import {spawnSync} from 'node:child_process';

export function copyTextToClipboard(text: string): boolean {
	if (!text) {
		return false;
	}
	const candidates: string[][] =
		process.platform === 'win32'
			? [['clip']]
			: process.platform === 'darwin'
				? [['pbcopy']]
				: [
						['wl-copy'],
						['xclip', '-selection', 'clipboard'],
						['xsel', '--clipboard', '--input'],
					];
	for (const [command, ...args] of candidates) {
		const result = spawnSync(command, args, {
			input: text,
			stdio: ['pipe', 'ignore', 'ignore'],
		});
		if (!result.error && result.status === 0) {
			return true;
		}
	}
	return false;
}
