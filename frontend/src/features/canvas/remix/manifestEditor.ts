export interface ManifestEditorState { revision: number; previewRevision: number | null; unresolvedRequiredEvents?: string[]; conflicts?: string[] }
export function markPreviewStale(state: Pick<ManifestEditorState, 'revision' | 'previewRevision'>): Pick<ManifestEditorState, 'revision' | 'previewRevision'> { return { revision: state.revision, previewRevision: null }; }
export function canExportManifest(state: Pick<ManifestEditorState, 'unresolvedRequiredEvents' | 'conflicts'>): boolean { return !(state.unresolvedRequiredEvents?.length || state.conflicts?.length); }
