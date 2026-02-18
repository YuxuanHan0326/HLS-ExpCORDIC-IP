#include "exp_cordic.h"

#include <cmath>
#include <cstdint>
#include <cstdio>

#ifndef TB_N_SAMPLES
#define TB_N_SAMPLES 200000
#endif

static const double K_T95_ONE_SIDED = 1.6448536269514722;

static uint32_t rng_state = 0x13579BDFu;

static uint32_t xorshift32() {
    uint32_t x = rng_state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    rng_state = x;
    return x;
}

static int uniform_int_closed(int lo, int hi) {
    const uint32_t span = (uint32_t)(hi - lo + 1);
    const uint32_t threshold = (uint32_t)(-span) % span;
    uint32_t r = 0;
    do {
        r = xorshift32();
    } while (r < threshold);
    return lo + (int)(r % span);
}

static int run_underflow_checks() {
    const exp_in_t tests[] = {exp_in_t(-8.5), exp_in_t(-9.0), exp_in_t(-12.0)};
    const int num_tests = (int)(sizeof(tests) / sizeof(tests[0]));

    for (int i = 0; i < num_tests; ++i) {
        const exp_out_t y = exp_cordic_ip(tests[i]);
        if (y != exp_out_t(0)) {
            std::printf("UNDERFLOW_FAIL x=%f y=%f\n", (double)tests[i], (double)y);
            return 1;
        }
    }
    return 0;
}

int main() {
    if (run_underflow_checks() != 0) {
        return 1;
    }

    const int frac_bits = EXP_IN_WL - EXP_IN_IWL;
    const int q_min = -(8 << frac_bits);
    const int q_max = 0;
    const double lsb = std::ldexp(1.0, -frac_bits);

    double mean_e2 = 0.0;
    double m2_e2 = 0.0;

    for (int i = 1; i <= TB_N_SAMPLES; ++i) {
        const int q = uniform_int_closed(q_min, q_max);
        const double x_d = (double)q * lsb;
        const exp_in_t x = exp_in_t(x_d);

        const double y_hat = (double)exp_cordic_ip(x);
        const double y_ref = std::exp((double)x);
        const double e2 = (y_hat - y_ref) * (y_hat - y_ref);

        const double delta = e2 - mean_e2;
        mean_e2 += delta / (double)i;
        const double delta2 = e2 - mean_e2;
        m2_e2 += delta * delta2;
    }

    const double mse = mean_e2;
    const double var_e2 = (TB_N_SAMPLES > 1) ? (m2_e2 / (double)(TB_N_SAMPLES - 1)) : 0.0;
    const double se_mse = std::sqrt(var_e2 / (double)TB_N_SAMPLES);
    const double ucb95 = mse + K_T95_ONE_SIDED * se_mse;

    std::printf(
        "RESULT iters=%d out_wl=%d out_iwl=%d N=%d mse=%.12e ucb95=%.12e\n",
        EXP_CORDIC_ITERS,
        EXP_OUT_WL,
        EXP_OUT_IWL,
        TB_N_SAMPLES,
        mse,
        ucb95);

    return 0;
}
