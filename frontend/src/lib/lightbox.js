import { reactive } from 'vue'

// Shared click-to-zoom lightbox (replaces the legacy delegated <img class="zoom">).
export const lightboxState = reactive({ src: '' })
export const openLightbox = (src) => { lightboxState.src = src }
export const closeLightbox = () => { lightboxState.src = '' }
