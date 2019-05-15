import torch

from torch.utils.data import DataLoader

from alignments.aer import AERSufficientStatistics
from alignments.models import AlignmentVAE
from alignments.data import PAD_TOKEN, create_batch

def create_model(hparams, vocab_src, vocab_tgt):
    return AlignmentVAE(dist=hparams.model_type,
                        prior_params=(hparams.prior_param_1, hparams.prior_param_2),
                        src_vocab_size=vocab_src.size(),
                        tgt_vocab_size=vocab_tgt.size(),
                        emb_size=hparams.emb_size,
                        hidden_size=hparams.hidden_size,
                        pad_idx=vocab_tgt[PAD_TOKEN],
                        pooling=hparams.pooling,
                        bidirectional=hparams.bidirectional,
                        num_layers=hparams.num_layers,
                        cell_type=hparams.cell_type)

def train_step(model, x, seq_mask_x, seq_len_x, y, seq_mask_y, seq_len_y, hparams, step):
    qa = model.approximate_posterior(x, seq_mask_x, seq_len_x, y, seq_mask_y, seq_len_y)
    pa = model.prior(seq_mask_x, seq_len_x, seq_mask_y)
    A = qa.rsample()
    logits = model(x, A)
    output_dict = model.loss(logits=logits, y=y, A=A, seq_mask_x=seq_mask_x,
                             seq_mask_y=seq_mask_y, pa=pa, qa=qa,
                             reduction="mean")
    return output_dict["loss"]

def validate(model, val_data, gold_alignments, vocab_src, vocab_tgt, device,
             hparams, step, summary_writer=None):

    model.eval()

    # Load the validation data.
    val_dl = DataLoader(val_data, shuffle=False, batch_size=hparams.batch_size,
                        num_workers=4)

    # TODO log % 0s and 1s
    total_correct_predictions = 0
    total_predictions = 0.
    num_sentences = 0
    total_ELBO = 0.
    total_KL = 0.
    alignments = []
    with torch.no_grad():
        for sen_x, sen_y in val_dl:

            # Infer the mean A | x, y.
            x, seq_mask_x, seq_len_x = create_batch(sen_x, vocab_src, device, include_null=False)
            y, seq_mask_y, seq_len_y = create_batch(sen_y, vocab_tgt, device)
            qa = model.approximate_posterior(x, seq_mask_x, seq_len_x, y, seq_mask_y, seq_len_y)
            if "bernoulli" in hparams.model_type:
                A = qa.mean.round() # [B, T_y, T_x]
            elif hparams.model_type == "hardkuma":
                zeros = torch.zeros_like(qa.base.a)
                ones = torch.ones_like(qa.base.a)
                p0 = qa.log_prob(zeros)
                p1 = qa.log_prob(ones)
                # We're ignoring continuous now.
                A = torch.where(p1 > p0, ones, zeros)
            else:
                raise NotImplementedError()

            # Store the alignment links. A link is (src_word, tgt_word), don't store null alignments. Sentences
            # start at 1 (1-indexed).
            for len_x_k, len_y_k, A_k in zip(seq_len_x, seq_len_y, A):
                links = set()
                for j, aj in enumerate(A_k[:len_y_k], 1):
                    for i, aji in enumerate(aj[:len_x_k], 1):
                        if aji > 0:
                            links.add((i, j))
                alignments.append(links)

            # Compute validation ELBO and KL.
            logits = model(x, qa.sample())
            pa = model.prior(seq_mask_x, seq_len_x, seq_mask_y)
            output_dict = model.loss(logits=logits, y=y, A=A, seq_mask_x=seq_mask_x,
                                     seq_mask_y=seq_mask_y, pa=pa, qa=qa, reduction="mean")
            total_ELBO += output_dict["ELBO"].item()
            total_KL += output_dict["KL"].item()
            num_sentences += x.size(0)

            # Compute statistics for validation accuracy.
            logits = model(x, A)
            predictions = torch.argmax(logits, dim=-1, keepdim=False)
            correct_predictions = (predictions == y) * seq_mask_y
            total_correct_predictions += correct_predictions.sum().item()
            total_predictions += seq_len_y.sum().item()

    val_ELBO = total_ELBO / num_sentences
    val_KL = total_KL / num_sentences

    # Compute AER.
    metric = AERSufficientStatistics()
    for a, gold_a in zip(alignments, gold_alignments):
        metric.update(sure=gold_a[0], probable=gold_a[1], predicted=a)
    val_aer = metric.aer()

    # Compute translation accuracy.
    val_accuracy = total_correct_predictions / total_predictions

    # Write validation summaries if a summary writer is given.
    if summary_writer is not None:
        # summary_writer.add_scalar("validation/NLL", val_NLL, step)
        # summary_writer.add_scalar("validation/perplexity", val_ppl, step)
        summary_writer.add_scalar("validation/KL", val_KL, step)
        summary_writer.add_scalar("validation/ELBO", val_ELBO, step)
        summary_writer.add_scalar("validation/AER", val_aer, step)
        summary_writer.add_scalar("validation/accuracy", val_accuracy, step)

    # Print validation results.
    print(f"validation accuracy = {val_accuracy:.2f} -- validation AER = {val_aer:.2f}"
          f" -- validation ELBO = {val_ELBO:,.2f} -- validation KL = {val_KL:,.2f}")

    return val_aer