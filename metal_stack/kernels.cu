#include "kernels.cuh"

namespace kernels {

void muli_vs(float *v, float s, int N) {
  int num_threads = min(N, MAX_THREADS_PER_BLOCK);
  int num_blocks = (N + MAX_THREADS_PER_BLOCK - 1) / MAX_THREADS_PER_BLOCK;
  k_muli_vs<<<num_blocks, num_threads>>>(v, s, N);
}

__global__ void k_muli_vs(float *v, float s, int N) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= N) return;

  v[idx] *= s;
}


void addi_vv(cublasHandle_t handle, float *v1, const float *v2,
        float v1_coeff, float v2_coeff, int N) {
  if (v1_coeff != 1.0)
    muli_vs(v1, v1_coeff, N);

  cublasSaxpy(handle, N, &v2_coeff, v2, 1, v1, 1);
}


void subtensor1(float *dst, const float *src, const float *idxs, int N, int D,
    float idx_scal_shift, float idx_vec_shift_coeff, float *idx_vec_shift) {
  int num_threads = min(D, MAX_THREADS_PER_BLOCK);
  int num_blocks = min(N, MAX_BLOCKS);
  k_subtensor1<<<num_blocks, num_threads>>>(dst, src, idxs, N, D,
      idx_scal_shift, idx_vec_shift_coeff, idx_vec_shift);
}

__global__ void k_subtensor1(float *dst, const float *src, const float *idxs,
    int N, int D, float idx_scal_shift, float idx_vec_shift_coeff,
    float *idx_vec_shift) {
  for (int i0 = blockIdx.x; i0 < N; i0 += gridDim.x) {
    float fsrc_idx = idxs[i0] + idx_scal_shift;
    fsrc_idx += idx_vec_shift_coeff * idx_vec_shift[i0];
    int src_idx = (int) fsrc_idx;

    int src_offset = src_idx * D;
    int dst_offset = i0 * D;
    for (int i1 = threadIdx.x; i1 < D; i1 += blockDim.x)
      dst[dst_offset + i1] = src[src_offset + i1];
  }
}


void set_subtensor1i_s(float *dst, float src, const float *idxs, int N,
    float idx_scal_shift, float idx_vec_shift_coeff, float *idx_vec_shift) {
  int num_threads = min(N, MAX_THREADS_PER_BLOCK);
  int num_blocks = (N + MAX_THREADS_PER_BLOCK - 1) / MAX_THREADS_PER_BLOCK;
  k_set_subtensor1i_s<<<num_blocks, num_threads>>>(
      dst, src, idxs, N, idx_scal_shift, idx_vec_shift_coeff, idx_vec_shift);
}

__global__ void k_set_subtensor1i_s(float *dst, float src, const float *idxs, int N,
    float idx_scal_shift, float idx_vec_shift_coeff, float *idx_vec_shift) {
  int k_idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (k_idx >= N) return;

  float fidx = idxs[k_idx] + idx_scal_shift;
  fidx += idx_vec_shift_coeff * idx_vec_shift[k_idx];
  int idx = (int) fidx;

  dst[idx] = src;
}


void switch_m(float *dst, const float *mask, const float *ift, const float *iff,
    int N, int D) {
  int num_threads = min(D, MAX_THREADS_PER_BLOCK);
  int num_blocks = min(N, MAX_BLOCKS);
  k_switch_m<<<num_blocks, num_threads>>>(dst, mask, ift, iff, N, D);
}

__global__ void k_switch_m(float *dst, const float *mask, const float *ift,
    const float *iff, int N, int D) {
  for (int i0 = blockIdx.x; i0 < N; i0 += gridDim.x) {
    const float *src = (int) mask[i0] ? ift : iff;
    int offset = i0 * D;
    for (int i1 = threadIdx.x; i1 < D; i1 += blockDim.x)
      dst[offset + i1] = src[offset + i1];
  }
}

}
