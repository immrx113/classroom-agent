import { useState, useRef, useCallback, useEffect } from 'react'

interface SpeechSynthesisOptions {
  lang?: string
  rate?: number
  pitch?: number
  volume?: number
}

/**
 * 语音合成 Hook（基于原生 Web Speech Synthesis API）
 *
 * 用于将 AI 回复朗读出来
 */
export function useSpeechSynthesis({
  lang = 'zh-CN',
  rate = 1.0,
  pitch = 1.0,
  volume = 1.0,
}: SpeechSynthesisOptions = {}) {
  const [isSpeaking, setIsSpeaking] = useState(false)
  const [isSupported, setIsSupported] = useState(false)
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null)

  useEffect(() => {
    setIsSupported('speechSynthesis' in window)
    return () => {
      if ('speechSynthesis' in window) {
        window.speechSynthesis.cancel()
      }
    }
  }, [])

  const speak = useCallback(
    (text: string) => {
      if (!isSupported) return

      // 停止当前朗读
      window.speechSynthesis.cancel()

      const utterance = new SpeechSynthesisUtterance(text)
      utterance.lang = lang
      utterance.rate = rate
      utterance.pitch = pitch
      utterance.volume = volume

      utterance.onstart = () => setIsSpeaking(true)
      utterance.onend = () => setIsSpeaking(false)
      utterance.onerror = () => setIsSpeaking(false)

      utteranceRef.current = utterance
      window.speechSynthesis.speak(utterance)
    },
    [isSupported, lang, rate, pitch, volume],
  )

  const stop = useCallback(() => {
    if (!isSupported) return
    window.speechSynthesis.cancel()
    setIsSpeaking(false)
  }, [isSupported])

  const toggle = useCallback(
    (text: string) => {
      if (isSpeaking) {
        stop()
      } else {
        speak(text)
      }
    },
    [isSpeaking, speak, stop],
  )

  return { isSupported, isSpeaking, speak, stop, toggle }
}
