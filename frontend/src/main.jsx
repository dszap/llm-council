import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import { installBrowserLogging } from './logger.js'

const browserLogger = installBrowserLogging()
browserLogger.start()

if (import.meta.hot) {
  import.meta.hot.dispose(() => browserLogger.stop())
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
