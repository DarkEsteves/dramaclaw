/** Extract only same-origin project media paths; the server rechecks authorization and file type. */
export function projectMediaPathFromUrl(project: string, url: string): string | null {
  try {
    const origin = typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
    const parsed = new URL(url, origin);
    if (parsed.origin !== origin) return null;
    const prefixes = [
      `/static/projects/${encodeURIComponent(project)}/`,
      `/api/v1/projects/${encodeURIComponent(project)}/media/`,
    ];
    const prefix = prefixes.find((item) => parsed.pathname.startsWith(item));
    if (!prefix) return null;
    const relative = decodeURIComponent(parsed.pathname.slice(prefix.length));
    if (!relative || relative.startsWith('/') || relative.split('/').includes('..')) return null;
    return relative;
  } catch {
    return null;
  }
}
