import { apiFetch } from './auth';

/** Документ, приложенный к свободному диалогу; агент видит его в папке materials/. */
export type ChatFile = { id: string; name: string; size_bytes: number };

export const CHAT_FILE_ACCEPT = '.pdf,.doc,.docx,.ppt,.pptx,.txt,.md,.png,.jpg,.jpeg,.csv,.xlsx';

export function formatFileSize(bytes: number): string {
  return bytes >= 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} МБ`
    : `${Math.max(1, Math.round(bytes / 1024))} КБ`;
}

export function chatFileUrl(conversationId: string, fileId: string): string {
  return `/api/conversations/${conversationId}/files/${fileId}`;
}

async function errorDetail(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    return typeof body.detail === 'string' ? body.detail : fallback;
  } catch {
    return fallback;
  }
}

async function fileListFrom(response: Response, fallback: string): Promise<ChatFile[]> {
  if (!response.ok) {
    throw new Error(await errorDetail(response, fallback));
  }
  return (await response.json()) as ChatFile[];
}

/** Пустой диалог, чтобы приложить документы до первого сообщения. */
export async function createConversation(): Promise<string> {
  const response = await apiFetch('/api/conversations', { method: 'POST' });
  if (!response.ok) {
    throw new Error('Не удалось создать диалог');
  }
  return ((await response.json()) as { id: string }).id;
}

export async function listChatFiles(conversationId: string): Promise<ChatFile[]> {
  return fileListFrom(
    await apiFetch(`/api/conversations/${conversationId}/files`),
    'Не удалось загрузить список файлов',
  );
}

export async function uploadChatFile(conversationId: string, file: File): Promise<ChatFile[]> {
  const form = new FormData();
  form.append('file', file);
  return fileListFrom(
    await apiFetch(`/api/conversations/${conversationId}/files`, { method: 'POST', body: form }),
    'Файл не загружен',
  );
}

export async function removeChatFile(conversationId: string, fileId: string): Promise<ChatFile[]> {
  return fileListFrom(
    await apiFetch(chatFileUrl(conversationId, fileId), { method: 'DELETE' }),
    'Не удалось убрать файл',
  );
}
