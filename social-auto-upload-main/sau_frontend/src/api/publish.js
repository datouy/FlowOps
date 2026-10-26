import { http } from '@/utils/request'

export const publishApi = {
  checkCompliance: payload => http.post('/compliance/check', payload),
  submit: payload => http.post('/postVideo', payload),
  getPendingVerify: () => http.get('/verify/pending', null, { silent: true }),
  submitVerify: (requestId, code) => http.post('/verify/submit', {
    request_id: requestId,
    code,
  }),
  resendVerify: requestId => http.post('/verify/resend', { request_id: requestId }),
}
