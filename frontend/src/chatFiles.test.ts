import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatFileSize, uploadChatFile } from './chatFiles';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('uploadChatFile', () => {
  it('sends the file as multipart form and returns the new list', async () => {
    const listed = [{ id: 'f1', name: 'бриф.docx', size_bytes: 3 }];
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(listed), { status: 201 }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await uploadChatFile('c1', new File(['abc'], 'бриф.docx'));

    expect(result).toEqual(listed);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/conversations/c1/files');
    expect(init.method).toBe('POST');
    expect((init.body as FormData).get('file')).toBeInstanceOf(File);
  });

  it('surfaces the server reason for a rejected file', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: 'Файл больше 5 МБ' }), { status: 413 })),
    );

    await expect(uploadChatFile('c1', new File(['x'], 'big.pdf'))).rejects.toThrow('Файл больше 5 МБ');
  });
});

describe('formatFileSize', () => {
  it('rounds small files up to a kilobyte and shows megabytes', () => {
    expect(formatFileSize(8)).toBe('1 КБ');
    expect(formatFileSize(2.5 * 1024 * 1024)).toBe('2.5 МБ');
  });
});
