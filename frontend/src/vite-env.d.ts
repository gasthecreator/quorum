/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API?: "mock" | "live";
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
