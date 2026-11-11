import ky from 'ky'
import { API_BASE_URL } from '@/lib/constants'

export const apiClient = ky.create({
  prefix: API_BASE_URL,
  timeout: 30000,
  hooks: {
    beforeRequest: [
      ({ request }) => {
        const token = localStorage.getItem('access_token')
        if (token) {
          request.headers.set('Authorization', `Bearer ${token}`)
        }
      },
    ],
  },
})
