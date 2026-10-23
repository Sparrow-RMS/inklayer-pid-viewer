import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Fonts ship with the app rather than from Google Fonts, so a strict
// Content-Security-Policy (style-src 'self') or a network that blocks Google
// doesn't leave the viewer without its type and icons.
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import 'material-symbols/outlined.css'
import './index.css'
import Portal from './Portal.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Portal />
  </StrictMode>,
)
