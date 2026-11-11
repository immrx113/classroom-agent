import { useState, useRef, useCallback, useEffect } from 'react'

/* eslint-disable @typescript-eslint/no-explicit-any */

interface SpeechRecognitionOptions {
  lang?: string
  continuous?: boolean
  interimResults?: boolean
  onResult?: (transcript: string, isFinal: boolean) => void
  onError?: (error: string) => void
}

/**
 * 语音识别 Hook（基于原生 Web Speech API）
 *
 * 浏览器支持：Chrome / Edge / Safari（需 webkit 前缀）
 */
export function useSpeechRecognition({
  lang = 'zh-CN',
  continuous = true,
  interimResults = true,
  onResult,
  onError,
}: SpeechRecognitionOptions = {}) {
  const [isListening, setIsListening] = useState(false)
  const [transcript, setTranscript] = useState('')
  const [isSupported, setIsSupported] = useState(false)
  const recognitionRef = useRef<any>(null)
  const onResultRef = useRef(onResult)
  const onErrorRef = useRef(onError)

  useEffect(() => {
    onResultRef.current = onResult
  }, [onResult])
  useEffect(() => {
    onErrorRef.current = onError
  }, [onError])

  useEffect(() => {
    const SR =
      (window as any).SpeechRecognition ||
      (window as any).webkitSpeechRecognition

    if (!SR) {
      setIsSupported(false)
      return
    }

    setIsSupported(true)
    const recognition = new SR()
    recognition.lang = lang
    recognition.continuous = continuous
    recognition.interimResults = interimResults

    recognition.onresult = (event: any) => {
      let finalTranscript = ''
      let interimTranscript = ''

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i]
        if (result.isFinal) {
          finalTranscript += result[0].transcript
        } else {
          interimTranscript += result[0].transcript
        }
      }

      if (finalTranscript) {
        setTranscript((prev) => prev + finalTranscript)
        onResultRef.current?.(finalTranscript, true)
      }
      if (interimTranscript) {
        onResultRef.current?.(interimTranscript, false)
      }
    }

    recognition.onerror = (event: any) => {
      const errorMsg = event.error || '识别失败'
      // not-allowed / service-not-allowed 表示用户拒绝麦克风权限
      if (errorMsg === 'no-speech' || errorMsg === 'aborted') return
      onErrorRef.current?.(errorMsg)
    }

    // Chrome 在静默时会自动停止，这里自动重启保持连续
    recognition.onend = () => {
      if (recognitionRef.current?._shouldRestart) {
        try {
          recognition.start()
        } catch {
          // 重复 start 会抛错，忽略
        }
      } else {
        setIsListening(false)
      }
    }

    recognitionRef.current = recognition

    return () => {
      recognitionRef.current._shouldRestart = false
      recognition.abort()
    }
  }, [lang, continuous, interimResults])

  const start = useCallback(() => {
    if (!recognitionRef.current) return
    setTranscript('')
    recognitionRef.current._shouldRestart = true
    try {
      recognitionRef.current.start()
      setIsListening(true)
    } catch {
      // 已在运行中
    }
  }, [])

  const stop = useCallback(() => {
    if (!recognitionRef.current) return
    recognitionRef.current._shouldRestart = false
    recognitionRef.current.stop()
    setIsListening(false)
  }, [])

  const toggle = useCallback(() => {
    if (isListening) {
      stop()
    } else {
      start()
    }
  }, [isListening, start, stop])

  const reset = useCallback(() => {
    setTranscript('')
  }, [])

  return {
    isSupported,
    isListening,
    transcript,
    start,
    stop,
    toggle,
    reset,
  }
}
