/** Keep the consequential action behind confirmation; cancellation is side-effect free. */
export function authorizeExecution(
  live: boolean,
  description: string,
  action: () => void,
  confirm: (message: string) => boolean = message => window.confirm(message),
): boolean {
  if (live && !confirm(description)) return false;
  action();
  return true;
}
