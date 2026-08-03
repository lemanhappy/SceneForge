import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'
import { applyTheme, currentTheme } from './lib/theme.js'

applyTheme(currentTheme())
createApp(App).mount('#app')
