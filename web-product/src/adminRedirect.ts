export function adminRedirect(hash: string): string | null {
  return hash === '#admin' ? '/#admin/overview' : null;
}
