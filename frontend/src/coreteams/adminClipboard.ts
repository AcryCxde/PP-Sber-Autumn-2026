export async function copyRunId(id: string, clipboard: Pick<Clipboard, 'writeText'> | undefined): Promise<boolean> {
  if (!clipboard?.writeText) return false;
  try {
    await clipboard.writeText(id);
    return true;
  } catch {
    return false;
  }
}
