import torch
import torch.nn.functional as F
import matplotlib.pyplot as plt

from torch import nn
from torch import Tensor
from PIL import Image
from torchvision.transforms import Compose, Resize, ToTensor
from einops import rearrange, reduce, repeat
from einops.layers.torch import Rearrange, Reduce
from torchsummary import summary


# 输入测试
#-------------------------------------------------------------------------------------------------------------#
# img = Image.open('test.jpg')
# fig = plt.figure()
# plt.imshow(img)
# plt.show()
#
# # resize to ImageNet size
# transform = Compose([Resize((224, 224)), ToTensor()])
# x = transform(img)
# x = x.unsqueeze(0)  # 主要是为了添加batch这个维度

# 定义Patches Embedding层
#-------------------------------------------------------------------------------------------------------------#
class PatchEmbedding(nn.Module):
    def __init__(self, in_channels: int = 3, patch_size: int = 16, emb_size: int = 768, img_size: int = 224):
        self.patch_size = patch_size
        super().__init__()
        self.projection = nn.Sequential(
            # 使用一个卷积层而不是一个线性层 -> 性能增加
            nn.Conv2d(in_channels, emb_size, kernel_size=patch_size, stride=patch_size),
            Rearrange('b e (h) (w) -> b (h w) e'),
        )
        self.cls_token = nn.Parameter(torch.randn(1, 1, emb_size))
        # 位置编码信息，一共有(img_size // patch_size)**2 + 1(cls token)个位置向量
        # 这里有个问题，到底应该添加2D还是1D的信息呢，需不要考虑图片之间的相对位置，这里的影响不会很大。
        self.positions = nn.Parameter(torch.randn((img_size // patch_size) ** 2 + 1, emb_size))

    def forward(self, x: Tensor) -> Tensor:
        b, _, _, _ = x.shape
        x = self.projection(x)
        cls_tokens = repeat(self.cls_token, '() n e -> b n e', b=b)
        # 将cls token在维度1扩展到输入上
        x = torch.cat([cls_tokens, x], dim=1)
        # 添加位置编码
        # 这里运用了广播机制，把原来2维变成了3维
        x += self.positions
        return x
# 定义TransformerEncoder层
#-------------------------------------------------------------------------------------------------------------#
class MultiHeadAttention(nn.Module):
    def __init__(self, emb_size: int = 768, num_heads: int = 8, dropout: float = 0):
        super().__init__()
        self.emb_size = emb_size
        self.num_heads = num_heads
        # 使用单个矩阵一次性计算出queries,keys,values
        self.qkv = nn.Linear(emb_size, emb_size * 3)
        self.att_drop = nn.Dropout(dropout)
        self.projection = nn.Linear(emb_size, emb_size)

    def forward(self, x: Tensor, mask: Tensor = None) -> Tensor:
        # 将queries，keys和values划分为num_heads
        qkv = rearrange(self.qkv(x), "b n (h d qkv) -> (qkv) b h n d", h=self.num_heads, qkv=3)  # 划分到num_heads个头上


        queries, keys, values = qkv[0], qkv[1], qkv[2]


        # 在最后一个维度上相加
        energy = torch.einsum('bhqd, bhkd -> bhqk', queries, keys)  # batch, num_heads, query_len, key_len

        # 例如处理句子，句子的长度往往是不同的。为了能够将这些变长序列放入神经网络模型（如 Transformer 架构）进行批量处理，通常会将序列填充到相同的长度。Mask 用于标记哪些位置是原始数据，哪些位置是填充的数据。
        if mask is not None:
            fill_value = torch.finfo(torch.float32).min
            energy.mask_fill(~mask, fill_value)

        scaling = self.emb_size ** (1 / 2)

        att = F.softmax(energy, dim=-1) / scaling

        att = self.att_drop(att)


        # 在第三个维度上相加
        out = torch.einsum('bhal, bhlv -> bhav ', att, values)

        out = rearrange(out, "b h n d -> b n (h d)")

        out = self.projection(out)

        return out


# patches_embedded = PatchEmbedding()(x)
# print("patches_embedding's shape: ", patches_embedded.shape)
# MultiHeadAttention()(patches_embedded).shape


# 定义一个ResNet
#-------------------------------------------------------------------------------------------------------------#
class ResidualAdd(nn.Module):
    # 注意这里的fn是你之后要传入的模型，可以是一个也可以是多个。
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        res = x
        x = self.fn(x, **kwargs)
        x += res
        return x


# 定义一个MlP层
#-------------------------------------------------------------------------------------------------------------#
class FeedForwardBlock(nn.Sequential):
    def __init__(self, emb_size: int, expansion: int = 4, drop_p: float = 0.):
        super().__init__(
            nn.Linear(emb_size, expansion * emb_size),
            nn.GELU(),
            nn.Dropout(drop_p),
            nn.Linear(expansion * emb_size, emb_size),
        )

# 定义一个完整的Encoder
# -------------------------------------------------------------------------------------------------------------#
class TransformerEncoderBlock(nn.Sequential):
    def __init__(self,
                 emb_size: int = 768,
                 drop_p: float = 0.,
                 forward_expansion: int = 4,
                 forward_drop_p: float = 0.,
                 ** kwargs):
        super().__init__(
            ResidualAdd(nn.Sequential(
                nn.LayerNorm(emb_size),
                MultiHeadAttention(emb_size, **kwargs),
                nn.Dropout(drop_p)
            )),
            ResidualAdd(nn.Sequential(
                nn.LayerNorm(emb_size),
                FeedForwardBlock(
                    emb_size, expansion=forward_expansion, drop_p=forward_drop_p),
                nn.Dropout(drop_p)
            )
            ))


# -------------------------------------------------------------------------------------------------------------#
class TransformerEncoder(nn.Sequential):
    def __init__(self, depth: int = 12, **kwargs):
        super().__init__(*[TransformerEncoderBlock(**kwargs) for _ in range(depth)])

# -------------------------------------------------------------------------------------------------------------#
class ClassificationHead(nn.Sequential):
    def __init__(self, emb_size: int = 768, n_classes: int = 1000):
        super().__init__(
            Reduce('b n e -> b e', reduction='mean'),
            nn.LayerNorm(emb_size),
            nn.Linear(emb_size, n_classes))

# 完整的ViT
# -------------------------------------------------------------------------------------------------------------#
class ViT(nn.Sequential):
    def __init__(self,
                in_channels: int = 3,
                patch_size: int = 16,
                emb_size: int = 768,
                img_size: int = 224,
                depth: int = 12,
                n_classes: int = 1000,
                **kwargs):
        super().__init__(
            PatchEmbedding(in_channels, patch_size, emb_size, img_size),
            TransformerEncoder(depth, emb_size=emb_size, **kwargs),
            ClassificationHead(emb_size, n_classes)
        )

# 计算参数
# -------------------------------------------------------------------------------------------------------------#
if __name__ == '__main__':
    model = ViT()
    summary(model, input_size=[(3, 224, 224)], batch_size=1, device="cpu")


