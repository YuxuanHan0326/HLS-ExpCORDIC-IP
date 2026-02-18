#include "exp_cordic.h"

#if EXP_CORDIC_ITERS < 1
#error "EXP_CORDIC_ITERS must be >= 1"
#endif
#if EXP_CORDIC_ITERS > 20
#error "EXP_CORDIC_ITERS must be <= 20 (table support limit)"
#endif

namespace {

// ============================================================================
// CORDIC Constants
// ============================================================================
static const exp_int_t kLn2 = exp_int_t(0.6931471805599453);

// shift_seq uses hyperbolic CORDIC repeated iterations at s=4 and s=13.
// The array length follows EXP_CORDIC_ITERS to trim unused constants.
static const unsigned char kShiftSeq[EXP_CORDIC_ITERS] = {
#if EXP_CORDIC_ITERS >= 1
    1,
#endif
#if EXP_CORDIC_ITERS >= 2
    2,
#endif
#if EXP_CORDIC_ITERS >= 3
    3,
#endif
#if EXP_CORDIC_ITERS >= 4
    4,
#endif
#if EXP_CORDIC_ITERS >= 5
    4,
#endif
#if EXP_CORDIC_ITERS >= 6
    5,
#endif
#if EXP_CORDIC_ITERS >= 7
    6,
#endif
#if EXP_CORDIC_ITERS >= 8
    7,
#endif
#if EXP_CORDIC_ITERS >= 9
    8,
#endif
#if EXP_CORDIC_ITERS >= 10
    9,
#endif
#if EXP_CORDIC_ITERS >= 11
    10,
#endif
#if EXP_CORDIC_ITERS >= 12
    11,
#endif
#if EXP_CORDIC_ITERS >= 13
    12,
#endif
#if EXP_CORDIC_ITERS >= 14
    13,
#endif
#if EXP_CORDIC_ITERS >= 15
    13,
#endif
#if EXP_CORDIC_ITERS >= 16
    14,
#endif
#if EXP_CORDIC_ITERS >= 17
    15,
#endif
#if EXP_CORDIC_ITERS >= 18
    16,
#endif
#if EXP_CORDIC_ITERS >= 19
    17,
#endif
#if EXP_CORDIC_ITERS >= 20
    18,
#endif
};

// atanh(2^-shift_seq[i]), length follows EXP_CORDIC_ITERS.
static const exp_int_t kAtanhTbl[EXP_CORDIC_ITERS] = {
#if EXP_CORDIC_ITERS >= 1
    exp_int_t(0.5493061443340549),
#endif
#if EXP_CORDIC_ITERS >= 2
    exp_int_t(0.25541281188299536),
#endif
#if EXP_CORDIC_ITERS >= 3
    exp_int_t(0.12565721414045306),
#endif
#if EXP_CORDIC_ITERS >= 4
    exp_int_t(0.06258157147700301),
#endif
#if EXP_CORDIC_ITERS >= 5
    exp_int_t(0.06258157147700301),
#endif
#if EXP_CORDIC_ITERS >= 6
    exp_int_t(0.031260178490666993),
#endif
#if EXP_CORDIC_ITERS >= 7
    exp_int_t(0.015626271752052213),
#endif
#if EXP_CORDIC_ITERS >= 8
    exp_int_t(0.007812658951540421),
#endif
#if EXP_CORDIC_ITERS >= 9
    exp_int_t(0.0039062698683968262),
#endif
#if EXP_CORDIC_ITERS >= 10
    exp_int_t(0.0019531274835325502),
#endif
#if EXP_CORDIC_ITERS >= 11
    exp_int_t(0.00097656281044103594),
#endif
#if EXP_CORDIC_ITERS >= 12
    exp_int_t(0.00048828128880511288),
#endif
#if EXP_CORDIC_ITERS >= 13
    exp_int_t(0.00024414062985063861),
#endif
#if EXP_CORDIC_ITERS >= 14
    exp_int_t(0.00012207031310632982),
#endif
#if EXP_CORDIC_ITERS >= 15
    exp_int_t(0.00012207031310632982),
#endif
#if EXP_CORDIC_ITERS >= 16
    exp_int_t(0.00006103515632579122),
#endif
#if EXP_CORDIC_ITERS >= 17
    exp_int_t(0.000030517578134473901),
#endif
#if EXP_CORDIC_ITERS >= 18
    exp_int_t(0.000015258789063684237),
#endif
#if EXP_CORDIC_ITERS >= 19
    exp_int_t(0.0000076293945313980292),
#endif
#if EXP_CORDIC_ITERS >= 20
    exp_int_t(0.0000038146972656435034)
#endif
};

// K(iters) = product_{i=0..iters-1}(sqrt(1 - 2^(-2*shift_seq[i]))), INV_K = 1/K.
// Strategy A: initialize X0 = INV_K(iters), Y0 = 0 so output is directly X + Y.
#if EXP_CORDIC_ITERS == 1
static const exp_int_t kInvK = exp_int_t(1.1547005383792517);
#elif EXP_CORDIC_ITERS == 2
static const exp_int_t kInvK = exp_int_t(1.1925695879998879);
#elif EXP_CORDIC_ITERS == 3
static const exp_int_t kInvK = exp_int_t(1.2019971622805570);
#elif EXP_CORDIC_ITERS == 4
static const exp_int_t kInvK = exp_int_t(1.2043517133368051);
#elif EXP_CORDIC_ITERS == 5
static const exp_int_t kInvK = exp_int_t(1.2067108766424417);
#elif EXP_CORDIC_ITERS == 6
static const exp_int_t kInvK = exp_int_t(1.2073005228426155);
#elif EXP_CORDIC_ITERS == 7
static const exp_int_t kInvK = exp_int_t(1.2074479253854811);
#elif EXP_CORDIC_ITERS == 8
static const exp_int_t kInvK = exp_int_t(1.2074847754587470);
#elif EXP_CORDIC_ITERS == 9
static const exp_int_t kInvK = exp_int_t(1.2074939879419180);
#elif EXP_CORDIC_ITERS == 10
static const exp_int_t kInvK = exp_int_t(1.2074962910605143);
#elif EXP_CORDIC_ITERS == 11
static const exp_int_t kInvK = exp_int_t(1.2074968668400261);
#elif EXP_CORDIC_ITERS == 12
static const exp_int_t kInvK = exp_int_t(1.2074970107848955);
#elif EXP_CORDIC_ITERS == 13
static const exp_int_t kInvK = exp_int_t(1.2074970467711124);
#elif EXP_CORDIC_ITERS == 14
static const exp_int_t kInvK = exp_int_t(1.2074970557676665);
#elif EXP_CORDIC_ITERS == 15
static const exp_int_t kInvK = exp_int_t(1.2074970647642207);
#elif EXP_CORDIC_ITERS == 16
static const exp_int_t kInvK = exp_int_t(1.2074970670133593);
#elif EXP_CORDIC_ITERS == 17
static const exp_int_t kInvK = exp_int_t(1.2074970675756440);
#elif EXP_CORDIC_ITERS == 18
static const exp_int_t kInvK = exp_int_t(1.2074970677162151);
#elif EXP_CORDIC_ITERS == 19
static const exp_int_t kInvK = exp_int_t(1.2074970677513579);
#elif EXP_CORDIC_ITERS == 20
static const exp_int_t kInvK = exp_int_t(1.2074970677601435);
#else
#error "INV_K table supports EXP_CORDIC_ITERS in [1, 20]"
#endif

// ============================================================================
// Range Reduction Tables for x in [-8, 0]
// ============================================================================
// {-ln2, -2ln2, ..., -12ln2}
static const exp_int_t kNegLn2Thresh[12] = {
    exp_int_t(-0.6931471805599453), exp_int_t(-1.3862943611198906),
    exp_int_t(-2.0794415416798357), exp_int_t(-2.7725887222397811),
    exp_int_t(-3.4657359027997265), exp_int_t(-4.1588830833596715),
    exp_int_t(-4.8520302639196169), exp_int_t(-5.5451774444795623),
    exp_int_t(-6.2383246250395077), exp_int_t(-6.9314718055994531),
    exp_int_t(-7.6246189861593985), exp_int_t(-8.3177661667193430)};

// {0*ln2, 1*ln2, ..., 12*ln2}
static const exp_int_t kLn2Mul[13] = {
    exp_int_t(0.0000000000000000), exp_int_t(0.6931471805599453),
    exp_int_t(1.3862943611198906), exp_int_t(2.0794415416798357),
    exp_int_t(2.7725887222397811), exp_int_t(3.4657359027997265),
    exp_int_t(4.1588830833596715), exp_int_t(4.8520302639196169),
    exp_int_t(5.5451774444795623), exp_int_t(6.2383246250395077),
    exp_int_t(6.9314718055994531), exp_int_t(7.6246189861593985),
    exp_int_t(8.3177661667193430)};

// ============================================================================
// Helpers
// ============================================================================
static exp_k_t k_from_range_reduce_nomul(exp_int_t x_int) {
#pragma HLS INLINE
    if (x_int >= exp_int_t(0.0)) {
        return exp_k_t(0);
    }
    if (x_int >= kNegLn2Thresh[0]) {
        return exp_k_t(-1);
    }
    if (x_int >= kNegLn2Thresh[1]) {
        return exp_k_t(-2);
    }
    if (x_int >= kNegLn2Thresh[2]) {
        return exp_k_t(-3);
    }
    if (x_int >= kNegLn2Thresh[3]) {
        return exp_k_t(-4);
    }
    if (x_int >= kNegLn2Thresh[4]) {
        return exp_k_t(-5);
    }
    if (x_int >= kNegLn2Thresh[5]) {
        return exp_k_t(-6);
    }
    if (x_int >= kNegLn2Thresh[6]) {
        return exp_k_t(-7);
    }
    if (x_int >= kNegLn2Thresh[7]) {
        return exp_k_t(-8);
    }
    if (x_int >= kNegLn2Thresh[8]) {
        return exp_k_t(-9);
    }
    if (x_int >= kNegLn2Thresh[9]) {
        return exp_k_t(-10);
    }
    if (x_int >= kNegLn2Thresh[10]) {
        return exp_k_t(-11);
    }
    return exp_k_t(-12);
}

static exp_int_t scale_by_pow2_k(exp_int_t v, int k_i) {
#pragma HLS INLINE
    switch (k_i) {
        case -12: return (v >> 12);
        case -11: return (v >> 11);
        case -10: return (v >> 10);
        case -9: return (v >> 9);
        case -8: return (v >> 8);
        case -7: return (v >> 7);
        case -6: return (v >> 6);
        case -5: return (v >> 5);
        case -4: return (v >> 4);
        case -3: return (v >> 3);
        case -2: return (v >> 2);
        case -1: return (v >> 1);
        case 0: return v;
        default: return v;
    }
}

}  // namespace

exp_out_t exp_cordic_ip(exp_in_t x) {
#pragma HLS ARRAY_PARTITION variable = kNegLn2Thresh complete dim = 1
#pragma HLS ARRAY_PARTITION variable = kLn2Mul complete dim = 1
#pragma HLS ARRAY_PARTITION variable = kShiftSeq complete dim = 1
#pragma HLS ARRAY_PARTITION variable = kAtanhTbl complete dim = 1
#pragma HLS PIPELINE

    if (x < exp_in_t(-8.0)) {
        return exp_out_t(0);
    }

    const exp_int_t x_int = (exp_int_t)x;

    // Multiplier-free range reduction in [-8, 0]:
    // k = floor(x/ln2), r = x - k*ln2 = x + (-k)*ln2.
    exp_k_t k = k_from_range_reduce_nomul(x_int);
    ap_uint<4> idx = ap_uint<4>(-k);
    exp_int_t r = x_int + kLn2Mul[(int)idx];

    // Guard against edge quantization at boundaries.
    if (r < exp_int_t(0)) {
        r += kLn2;
        k -= 1;
    } else if (r >= kLn2) {
        r -= kLn2;
        k += 1;
    }

    exp_int_t X = kInvK;
    exp_int_t Y = exp_int_t(0);
    exp_int_t Z = r;

    for (int i = 0; i < EXP_CORDIC_ITERS; ++i) {
#pragma HLS UNROLL
        const int s = (int)kShiftSeq[i];
        const exp_int_t X_old = X;
        const exp_int_t Y_old = Y;

        if (Z >= exp_int_t(0)) {
            X = X_old + (Y_old >> s);
            Y = Y_old + (X_old >> s);
            Z = Z - kAtanhTbl[i];
        } else {
            X = X_old - (Y_old >> s);
            Y = Y_old - (X_old >> s);
            Z = Z + kAtanhTbl[i];
        }
    }

    const exp_int_t exp_r = X + Y;
    const int k_i = (int)k;
    // exp(x) = exp(r) * 2^k.
    const exp_int_t exp_x = scale_by_pow2_k(exp_r, k_i);

    return (exp_out_t)exp_x;
}
