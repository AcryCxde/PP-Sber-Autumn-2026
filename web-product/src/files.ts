import type { InputFile } from "./domain.ts";
function db(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("coreteams-product-files-v2", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("files");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(new Error("Хранилище файлов недоступно"));
  });
}
async function transaction<T>(
  mode: IDBTransactionMode,
  work: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  const database = await db();
  return new Promise((resolve, reject) => {
    const tx = database.transaction("files", mode),
      req = work(tx.objectStore("files"));
    let value: T;
    req.onsuccess = () => {
      value = req.result;
    };
    req.onerror = () => tx.abort();
    tx.oncomplete = () => {
      database.close();
      resolve(value);
    };
    tx.onabort = tx.onerror = () => {
      database.close();
      reject(
        new Error(
          "Не удалось сохранить или прочитать файл. Проверьте место и разрешения браузера.",
        ),
      );
    };
  });
}
export async function keepFile(
  projectId: string,
  file: File,
): Promise<InputFile> {
  const id = crypto.randomUUID(),
    buffer = await file.arrayBuffer(),
    digest = await crypto.subtle.digest("SHA-256", buffer);
  const sha256 = Array.from(new Uint8Array(digest))
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
  await transaction("readwrite", (store) =>
    store.put(file, `${projectId}/${id}`),
  );
  return {
    id,
    name: file.name,
    size: file.size,
    type: file.type || "application/octet-stream",
    sha256,
  };
}
export async function getFile(projectId: string, id: string): Promise<Blob> {
  const result = await transaction("readonly", (store) =>
    store.get(`${projectId}/${id}`),
  );
  if (!(result instanceof Blob))
    throw new Error(
      "Содержимое файла отсутствует в этом браузере. Добавьте его заново до запуска.",
    );
  return result;
}
export async function removeFile(projectId: string, id: string) {
  await transaction("readwrite", (store) => store.delete(`${projectId}/${id}`));
}
