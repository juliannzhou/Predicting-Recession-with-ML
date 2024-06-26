# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

from typing import List, Optional, Tuple

import torch
import torch.nn as nn
from gluonts.core.component import validated
from gluonts.itertools import prod
from gluonts.model import Input, InputSpec
from gluonts.time_feature import get_lags_for_frequency
from gluonts.torch.distributions import DistributionOutput
from gluonts.torch.modules.feature import FeatureEmbedder
from gluonts.torch.modules.loss import DistributionLoss, NegativeLogLikelihood
from gluonts.torch.scaler import MeanScaler, NOPScaler, Scaler, StdScaler
from gluonts.torch.util import repeat_along_dim, unsqueeze_expand

from pts.modules import StudentTOutput
from pts.util import lagged_sequence_values

from .rolling_std_scaler import RollingStdScaler


class ColdDeepARModel(nn.Module):
    """
    Module implementing the ColdDeepAR model, see [SFG17]_.

    *Note:* the code of this model is unrelated to the implementation behind
    `SageMaker's ColdDeepAR Forecasting Algorithm
    <https://docs.aws.amazon.com/sagemaker/latest/dg/ColdDeepAR.html>`_.

    Parameters
    ----------
    freq
        String indicating the sampling frequency of the data to be processed.
    context_length
        Length of the RNN unrolling prior to the forecast date.
    prediction_length
        Number of time points to predict.
    num_feat_dynamic_real
        Number of dynamic real features that will be provided to ``forward``.
    num_feat_static_real
        Number of static real features that will be provided to ``forward``.
    num_feat_static_cat
        Number of static categorical features that will be provided to
        ``forward``.
    cardinality
        List of cardinalities, one for each static categorical feature.
    embedding_dimension
        Dimension of the embedding space, one for each static categorical
        feature.
    num_layers
        Number of layers in the RNN.
    hidden_size
        Size of the hidden layers in the RNN.
    dropout_rate
        Dropout rate to be applied at training time.
    distr_output
        Type of distribution to be output by the model at each time step
    lags_seq
        Indices of the lagged observations that the RNN takes as input. For
        example, ``[1]`` indicates that the RNN only takes the observation at
        time ``t-1`` to produce the output for time ``t``; instead,
        ``[1, 25]`` indicates that the RNN takes observations at times ``t-1``
        and ``t-25`` as input.
    scaling
        Whether to apply mean scaling to the observations (target).
    default_scale
        Default scale that is applied if the context length window is
        completely unobserved. If not set, the scale in this case will be
        the mean scale in the batch.
    num_parallel_samples
        Number of samples to produce when unrolling the RNN in the prediction
        time range.
    """

    @validated()
    def __init__(
        self,
        freq: str,
        context_length: int,
        prediction_length: int,
        input_size: int = 1,
        num_feat_dynamic_real: int = 1,
        num_feat_static_real: int = 1,
        num_feat_static_cat: int = 1,
        cardinality: List[int] = [1],
        embedding_dimension: Optional[List[int]] = None,
        num_layers: int = 2,
        hidden_size: int = 40,
        dropout_rate: float = 0.1,
        distr_output: DistributionOutput = StudentTOutput(),
        lags_seq: Optional[List[int]] = None,
        num_parallel_samples: int = 100,
    ) -> None:
        super().__init__()

        assert distr_output.event_shape == () if input_size == 1 else (input_size,)
        assert num_feat_dynamic_real > 0
        assert num_feat_static_real > 0
        assert num_feat_static_cat > 0
        assert len(cardinality) == num_feat_static_cat
        assert (
            embedding_dimension is None
            or len(embedding_dimension) == num_feat_static_cat
        )

        self.context_length = context_length
        self.prediction_length = prediction_length
        self.input_size = input_size
        self.distr_output = distr_output
        self.param_proj = distr_output.get_args_proj(hidden_size)
        self.num_feat_dynamic_real = num_feat_dynamic_real
        self.num_feat_static_cat = num_feat_static_cat
        self.num_feat_static_real = num_feat_static_real
        self.embedding_dimension = (
            embedding_dimension
            if embedding_dimension is not None or cardinality is None
            else [min(50, (cat + 1) // 2) for cat in cardinality]
        )
        self.lags_seq = lags_seq or get_lags_for_frequency(freq_str=freq)
        self.lags_seq = [l - 1 for l in self.lags_seq]
        self.num_parallel_samples = num_parallel_samples
        self.past_length = self.context_length + max(self.lags_seq)
        self.embedder = FeatureEmbedder(
            cardinalities=cardinality,
            embedding_dims=self.embedding_dimension,
        )

        self.rnn_input_size = self.input_size * len(self.lags_seq) + hidden_size

        self.cov_rnn = nn.LSTM(
            input_size=self._number_of_features,
            hidden_size=hidden_size,
            num_layers=num_layers,
            dropout=dropout_rate,
            batch_first=True,
        )

        self.scaler = nn.Linear(hidden_size, 2 * self.input_size)

        self.rnn = nn.LSTM(
            input_size=self.rnn_input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            dropout=dropout_rate,
            batch_first=True,
        )

    def describe_inputs(self, batch_size=1) -> InputSpec:
        return InputSpec(
            {
                "feat_static_cat": Input(
                    shape=(batch_size, self.num_feat_static_cat),
                    dtype=torch.long,
                ),
                "feat_static_real": Input(
                    shape=(batch_size, self.num_feat_static_real),
                    dtype=torch.float,
                ),
                "past_time_feat": Input(
                    shape=(
                        batch_size,
                        self._past_length,
                        self.num_feat_dynamic_real,
                    ),
                    dtype=torch.float,
                ),
                "past_target": Input(
                    shape=(batch_size, self._past_length)
                    if self.input_size == 1
                    else (batch_size, self._past_length, self.input_size),
                    dtype=torch.float,
                ),
                "past_observed_values": Input(
                    shape=(batch_size, self._past_length)
                    if self.input_size == 1
                    else (batch_size, self._past_length, self.input_size),
                    dtype=torch.float,
                ),
                "future_time_feat": Input(
                    shape=(
                        batch_size,
                        self.prediction_length,
                        self.num_feat_dynamic_real,
                    ),
                    dtype=torch.float,
                ),
            },
            zeros_fn=torch.zeros,
        )

    @property
    def _number_of_features(self) -> int:
        return (
            sum(self.embedding_dimension)
            + self.num_feat_dynamic_real
            + self.num_feat_static_real
        )

    @property
    def _past_length(self) -> int:
        return self.context_length + max(self.lags_seq)

    def prepare_rnn_input(
        self,
        loc,
        scale,
        cov_output,
        past_target: torch.Tensor,
        past_observed_values: torch.Tensor,
        future_length: int,
        future_target: Optional[torch.Tensor] = None,
    ):
        context = past_target[:, -self.context_length :, ...]
        observed_context = past_observed_values[:, -self.context_length :, ...]

        # input, loc, scale = self.scaler(context, observed_context)

        if future_length > 1:
            assert future_target is not None
            input = torch.cat(
                (context, future_target[:, : future_length - 1, ...]), dim=1
            )
        else:
            input = context
        prior_input = past_target[:, : -self.context_length, ...]

        lags = (
            lagged_sequence_values(self.lags_seq, prior_input, input, dim=1) - loc
        ) / scale

        return torch.cat((lags, cov_output), dim=-1)

    def prepare_cov_rnn_input(
        self,
        feat_static_cat: torch.Tensor,
        feat_static_real: torch.Tensor,
        past_time_feat: torch.Tensor,
        # past_target: torch.Tensor,
        # past_observed_values: torch.Tensor,
        future_time_feat: torch.Tensor,
        # future_target: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor,]:
        time_feat = torch.cat(
            (
                past_time_feat[:, -self.context_length + 1 :, ...],
                future_time_feat,
            ),
            dim=1,
        )

        embedded_cat = self.embedder(feat_static_cat)

        static_feat = torch.cat((embedded_cat, feat_static_real), dim=-1)
        expanded_static_feat = unsqueeze_expand(
            static_feat, dim=1, size=time_feat.shape[-2]
        )

        features = torch.cat((expanded_static_feat, time_feat), dim=-1)

        return features, static_feat

    def unroll_lagged_rnn(
        self,
        feat_static_cat: torch.Tensor,
        feat_static_real: torch.Tensor,
        past_time_feat: torch.Tensor,
        past_target: torch.Tensor,
        past_observed_values: torch.Tensor,
        future_time_feat: torch.Tensor,
        future_target: Optional[torch.Tensor] = None,
    ) -> Tuple[
        Tuple[torch.Tensor, ...],
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        Tuple[torch.Tensor, torch.Tensor],
    ]:
        """
        Applies the underlying RNN to the provided target data and covariates.

        Parameters
        ----------
        feat_static_cat
            Tensor of static categorical features,
            shape: ``(batch_size, num_feat_static_cat)``.
        feat_static_real
            Tensor of static real features,
            shape: ``(batch_size, num_feat_static_real)``.
        past_time_feat
            Tensor of dynamic real features in the past,
            shape: ``(batch_size, past_length, num_feat_dynamic_real)``.
        past_target
            Tensor of past target values,
            shape: ``(batch_size, past_length)``.
        past_observed_values
            Tensor of observed values indicators,
            shape: ``(batch_size, past_length)``.
        future_time_feat
            Tensor of dynamic real features in the future,
            shape: ``(batch_size, prediction_length, num_feat_dynamic_real)``.
        future_target
            (Optional) tensor of future target values,
            shape: ``(batch_size, prediction_length)``.

        Returns
        -------
        Tuple
            A tuple containing, in this order:
            - Parameters of the output distribution
            - Scaling factor applied to the target
            - Raw output of the RNN
            - Static input to the RNN
            - Output state from the RNN
        """

        features, static_feat = self.prepare_cov_rnn_input(
            feat_static_cat,
            feat_static_real,
            past_time_feat,
            future_time_feat,
        )

        cov_output, new_conv_state = self.cov_rnn(features)

        loc, scale = self.scaler(cov_output).chunk(2, dim=-1)
        scale = (scale + torch.sqrt(torch.square(scale) + 4.0)) / 2.0

        rnn_input = self.prepare_rnn_input(
            loc,
            scale,
            cov_output,
            past_target,
            past_observed_values,
            future_time_feat.shape[-2],
            future_target,
        )

        output, new_rnn_state = self.rnn(rnn_input)

        params = self.param_proj(output)
        return (
            params,
            loc.squeeze(-1),
            scale.squeeze(-1),
            output,
            static_feat,
            new_conv_state,
            new_rnn_state,
        )

    @torch.jit.ignore
    def output_distribution(
        self, params, loc=None, scale=None, trailing_n=None
    ) -> torch.distributions.Distribution:
        """
        Instantiate the output distribution

        Parameters
        ----------
        params
            Tuple of distribution parameters.
        loc
            (Optional) distribution shift tensor.
        scale
            (Optional) distribution scale tensor.
        trailing_n
            If set, the output distribution is created only for the last
            ``trailing_n`` time points.

        Returns
        -------
        torch.distributions.Distribution
            Output distribution from the model.
        """
        sliced_params = params
        sliced_loc = loc
        sliced_scale = scale
        if trailing_n is not None:
            sliced_params = [p[:, -trailing_n:] for p in params]
            sliced_loc = loc[:, -trailing_n:]
            sliced_scale = scale[:, -trailing_n:]
        return self.distr_output.distribution(
            sliced_params, loc=sliced_loc, scale=sliced_scale
        )

    def forward(
        self,
        feat_static_cat: torch.Tensor,
        feat_static_real: torch.Tensor,
        past_time_feat: torch.Tensor,
        past_target: torch.Tensor,
        past_observed_values: torch.Tensor,
        future_time_feat: torch.Tensor,
        num_parallel_samples: Optional[int] = None,
    ) -> torch.Tensor:
        """
        Invokes the model on input data, and produce outputs future samples.

        Parameters
        ----------
        feat_static_cat
            Tensor of static categorical features,
            shape: ``(batch_size, num_feat_static_cat)``.
        feat_static_real
            Tensor of static real features,
            shape: ``(batch_size, num_feat_static_real)``.
        past_time_feat
            Tensor of dynamic real features in the past,
            shape: ``(batch_size, past_length, num_feat_dynamic_real)``.
        past_target
            Tensor of past target values,
            shape: ``(batch_size, past_length)``.
        past_observed_values
            Tensor of observed values indicators,
            shape: ``(batch_size, past_length)``.
        future_time_feat
            (Optional) tensor of dynamic real features in the past,
            shape: ``(batch_size, prediction_length, num_feat_dynamic_real)``.
        num_parallel_samples
            How many future samples to produce.
            By default, self.num_parallel_samples is used.
        """
        if num_parallel_samples is None:
            num_parallel_samples = self.num_parallel_samples

        (
            params,
            loc,
            scale,
            _,
            static_feat,
            cov_state,
            rnn_state,
        ) = self.unroll_lagged_rnn(
            feat_static_cat,
            feat_static_real,
            past_time_feat,
            past_target,
            past_observed_values,
            future_time_feat[:, :1],
        )

        repeated_scale = scale.repeat_interleave(repeats=num_parallel_samples, dim=0)
        repeated_loc = loc.repeat_interleave(repeats=num_parallel_samples, dim=0)

        repeated_static_feat = static_feat.repeat_interleave(
            repeats=num_parallel_samples, dim=0
        ).unsqueeze(1)
        # repeated_past_target = (
        #     past_target.repeat_interleave(repeats=num_parallel_samples, dim=0)
        #     - repeated_loc
        # ) / repeated_scale
        repeated_past_target = past_target.repeat_interleave(
            repeats=num_parallel_samples, dim=0
        )
        repeated_time_feat = future_time_feat.repeat_interleave(
            repeats=num_parallel_samples, dim=0
        )
        repeated_cov_state = [
            s.repeat_interleave(repeats=num_parallel_samples, dim=1) for s in cov_state
        ]
        repeated_rnn_state = [
            s.repeat_interleave(repeats=num_parallel_samples, dim=1) for s in rnn_state
        ]

        repeated_params = [
            s.repeat_interleave(repeats=num_parallel_samples, dim=0) for s in params
        ]
        distr = self.output_distribution(
            repeated_params, trailing_n=1, loc=repeated_loc, scale=repeated_scale
        )
        next_sample = distr.sample()
        future_samples = [next_sample]

        for k in range(1, self.prediction_length):
            # scaled_next_sample = (next_sample - repeated_loc) / repeated_scale
            next_features = torch.cat(
                (repeated_static_feat, repeated_time_feat[:, k : k + 1]),
                dim=-1,
            )
            cov_output, repeated_cov_state = self.cov_rnn(
                next_features, repeated_cov_state
            )
            repeated_loc, scale = self.scaler(cov_output).chunk(2, dim=-1)
            repeated_scale = (scale + torch.sqrt(torch.square(scale) + 4.0)) / 2.0

            next_lags = (
                lagged_sequence_values(
                    self.lags_seq, repeated_past_target, next_sample, dim=1
                )
                - repeated_loc
            ) / scale

            rnn_input = torch.cat((next_lags, cov_output), dim=-1)

            output, repeated_rnn_state = self.rnn(rnn_input, repeated_rnn_state)

            repeated_past_target = torch.cat((repeated_past_target, next_sample), dim=1)

            params = self.param_proj(output)
            distr = self.output_distribution(
                params, loc=repeated_loc.squeeze(-1), scale=repeated_scale.squeeze(-1)
            )
            next_sample = distr.sample()
            future_samples.append(next_sample)

        future_samples_concat = torch.cat(future_samples, dim=1).reshape(
            (-1, num_parallel_samples, self.prediction_length, self.input_size)
        )

        return future_samples_concat.squeeze(-1)

    def log_prob(
        self,
        feat_static_cat: torch.Tensor,
        feat_static_real: torch.Tensor,
        past_time_feat: torch.Tensor,
        past_target: torch.Tensor,
        past_observed_values: torch.Tensor,
        future_time_feat: torch.Tensor,
        future_target: torch.Tensor,
    ) -> torch.Tensor:
        return -self.loss(
            feat_static_cat=feat_static_cat,
            feat_static_real=feat_static_real,
            past_time_feat=past_time_feat,
            past_target=past_target,
            past_observed_values=past_observed_values,
            future_time_feat=future_time_feat,
            future_target=future_target,
            future_observed_values=torch.ones_like(future_target),
            loss=NegativeLogLikelihood(),
            future_only=True,
            aggregate_by=torch.sum,
        )

    def loss(
        self,
        feat_static_cat: torch.Tensor,
        feat_static_real: torch.Tensor,
        past_time_feat: torch.Tensor,
        past_target: torch.Tensor,
        past_observed_values: torch.Tensor,
        future_time_feat: torch.Tensor,
        future_target: torch.Tensor,
        future_observed_values: torch.Tensor,
        loss: DistributionLoss = NegativeLogLikelihood(),
        future_only: bool = False,
        aggregate_by=torch.mean,
    ) -> torch.Tensor:
        extra_dims = len(future_target.shape) - len(past_target.shape)
        extra_shape = future_target.shape[:extra_dims]
        batch_shape = future_target.shape[: extra_dims + 1]

        repeats = prod(extra_shape)
        feat_static_cat = repeat_along_dim(feat_static_cat, 0, repeats)
        feat_static_real = repeat_along_dim(feat_static_real, 0, repeats)
        past_time_feat = repeat_along_dim(past_time_feat, 0, repeats)
        past_target = repeat_along_dim(past_target, 0, repeats)
        past_observed_values = repeat_along_dim(past_observed_values, 0, repeats)
        future_time_feat = repeat_along_dim(future_time_feat, 0, repeats)

        future_target_reshaped = future_target.reshape(
            -1,
            *future_target.shape[extra_dims + 1 :],
        )
        future_observed_reshaped = future_observed_values.reshape(
            -1,
            *future_observed_values.shape[extra_dims + 1 :],
        )

        params, loc, scale, _, _, _, _ = self.unroll_lagged_rnn(
            feat_static_cat,
            feat_static_real,
            past_time_feat,
            past_target,
            past_observed_values,
            future_time_feat,
            future_target_reshaped,
        )

        if future_only:
            distr = self.output_distribution(
                params, loc=loc, scale=scale, trailing_n=self.prediction_length
            )
            observed_values = (
                future_observed_reshaped.all(-1)
                if future_observed_reshaped.ndim == 3
                else future_observed_reshaped
            )
            loss_values = loss(distr, future_target_reshaped) * observed_values
        else:
            distr = self.output_distribution(params, loc=loc, scale=scale)
            context_target = past_target[:, -self.context_length + 1 :, ...]
            target = torch.cat(
                (context_target, future_target_reshaped),
                dim=1,
            )
            context_observed = past_observed_values[:, -self.context_length + 1 :, ...]
            observed_values = torch.cat(
                (context_observed, future_observed_reshaped), dim=1
            )
            observed_values = (
                observed_values.all(-1)
                if observed_values.ndim == 3
                else observed_values
            )
            loss_values = loss(distr, target) * observed_values

        return aggregate_by(loss_values, dim=(1,))
