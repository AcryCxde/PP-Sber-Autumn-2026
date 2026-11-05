import { useRef, useState } from 'react';
import { api, type Project } from './api';
import { errorText } from './actions';
import { useShell } from './context';
import { Icon } from './ui';

const ACCEPT = '.pdf,.doc,.docx,.ppt,.pptx,.txt,.md,.png,.jpg,.jpeg,.csv,.xlsx';

function formatSize(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} МБ` : `${Math.max(1, Math.round(bytes / 1024))} КБ`;
}

/** Загрузка материалов: файлы уходят на сервер и попадают к Claude в папку materials/. */
export function Upload({ project, onProject }: { project: Project; onProject: (project: Project) => void }) {
  const { toast } = useShell();
  const [files, setFiles] = useState(project.files);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const apply = (next: Project) => {
    setFiles(next.files);
    onProject(next);
  };

  const add = async (list: FileList | null) => {
    if (!list?.length) return;
    setBusy(true);
    try {
      for (const file of Array.from(list)) {
        try {
          apply(await api.upload(project.id, file));
        } catch (error) {
          const message = errorText(error, 'Файл не загружен');
          if (message) toast(`${file.name}: ${message}`);
        }
      }
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  const remove = async (fileId: string, name: string) => {
    try {
      apply(await api.removeFile(project.id, fileId));
    } catch (error) {
      const message = errorText(error, `Не удалось убрать ${name}`);
      if (message) toast(message);
    }
  };

  return (
    <div className="upload-box">
      <label>
        <Icon name="plus" />
        {busy ? 'Загружаем…' : 'Добавить файлы'}
        <input
          ref={inputRef}
          className="file-input"
          aria-label="Добавить файлы проекта"
          type="file"
          multiple
          accept={ACCEPT}
          disabled={busy}
          onChange={(event) => void add(event.target.files)}
        />
      </label>
      <div>
        {files.length ? (
          <ul className="file-list">
            {files.map((file) => (
              <li key={file.id}>
                {file.name} · {formatSize(file.size_bytes)}
                <button type="button" onClick={() => void remove(file.id, file.name)} aria-label={`Убрать ${file.name} из проекта`}>×</button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="helper">Файлов пока нет. Можно продолжить с описанием.</p>
        )}
      </div>
      <p className="helper">PDF, документы, презентации, таблицы, изображения — до 5 МБ каждый, до 20 файлов.<br />Claude прочитает их перед работой над каждым этапом.</p>
    </div>
  );
}
