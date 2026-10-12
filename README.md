# Descargalo — Pega el enlace y descarga

Web sencilla para descargar videos (MP4) y música (MP3) desde enlaces.
**Stack:** Python (FastAPI + yt-dlp) + React (Vite).

Incluye: **múltiples enlaces a la vez**, **progreso real** (% + velocidad + ETA),
**calidades con tamaño estimado**, **historial** en el navegador y **modo oscuro**.

## Estructura

```
backend/
  main.py            # API: /api/info, /api/jobs, /api/download, /api/health
  requirements.txt
  Dockerfile         # para despliegue (incluye ffmpeg)
frontend/
  src/App.jsx        # UI completa
  src/styles.css     # Sistema de diseño (claro/oscuro)
render.yaml          # despliegue en Render (API + web)
```

## Requisitos

- Python 3.10+ con `pip`
- Node 18+ con `npm`
- `ffmpeg` (recomendado, obligatorio para MP3)

En CachyOS / Arch:

```bash
sudo pacman -S python-pip ffmpeg nodejs npm
```

En Ubuntu / Debian:

```bash
sudo apt install python3-pip ffmpeg nodejs npm -y
```

## 1. Backend (Python)

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Verifica: http://127.0.0.1:8000/api/health

## 2. Frontend (React)

```bash
cd frontend
npm install
npm run dev
```

Abre http://localhost:5173. El proxy de Vite ya apunta `/api` al backend.

## Uso

1. Pega uno o varios enlaces (separados por coma o salto de línea).
2. Elige **Video MP4** o **Audio MP3** y la calidad (con tamaño estimado).
3. Pulsa **Descargar** y sigue el progreso en vivo. Al terminar se guarda solo.
4. Revisa tu **Historial** para repetir descargas. Cambia a **modo oscuro** con el botón de la luna.

### Opción A: todo en Render (la más fácil)

1. Sube el proyecto a GitHub.
2. En [render.com](https://render.com) → New → Blueprint → selecciona tu repo.
   El archivo `render.yaml` crea los dos servicios (API + web) automáticamente.
3. Si la URL de la API no es `descargalo-api.onrender.com`, edítala en
   `render.yaml` (línea `VITE_API_URL`) y redespliega la web.


